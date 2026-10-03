# -*- coding: utf-8 -*-
"""数据洞察图表（总览页，对标观远 agent 的数据分析页）。

与 insight_scan 的区别：
- insight_scan 只挑「异常」（离群 / 失衡 / 突变 / 空值）；
- 本模块生成「常规数据分析图表」，每个图表带一段白话解释，让非专业人员
  也能一眼看懂「数据在讲什么」。

设计取向：
- **聚焦重要业务数据**：优先识别制造业核心场景（良率 / 工序损耗 / 缺陷质量 /
  设备停机 / 库存安全 / 工单达成），而不是泛泛地「挑一张表随便画个柱状图」；
- **图表类型有创意**：仪表盘 gauge、漏斗图 funnel、旭日图 sunburst、帕累托图
  pareto、库存健康度、堆叠构成图 stacked-bar，尽量不用单调的柱状/饼图；
- 全部确定性计算，不调用 LLM；解释文案由数据规则生成。
- SQL 一律走 insight_scan._safe_execute（只读校验 + 行/列级 ACL 改写），
  与问答主流程同级别权限。
"""

from __future__ import annotations

import re
import statistics
import time

from agent.insight_scan import _safe_execute, _q, _q_table, _to_num
from agent.suggest import _CATEGORICAL_TYPES, _NUMERIC_TYPES, _norm_type

MAX_POINTS = 30   # 趋势图最多展示的时间点数
MAX_CHARTS = 6    # 单次返回的图表总数上限

# 总览图表 TTL 缓存（与 db/tools 元数据缓存同级，切库后 key 变化自动失效）。
# 每次进总览页都会触发全库扫描 + 多层图表生成（约 40 条 SQL），缓存避免重复计算。
_OVERVIEW_CACHE: dict = {"key": None, "ts": 0.0, "value": None}
_OVERVIEW_TTL = 30.0   # 秒


def _overview_cache_key() -> str | None:
    """缓存键：库连接信息 + **用户身份/ACL 指纹**。

    安全修复（P0）：原实现只用库连接信息做键。而这些图表数据是由 _safe_execute
    （内部会走行级/列级 ACL 改写）按**当前用户**过滤后生成的 —— 于是 30 秒 TTL 内
    管理员生成的图表会被受限用户直接命中，形成跨用户数据泄漏（ACL 被缓存绕过）。
    """
    try:
        from database import get_database_config
        cfg = get_database_config()
        base = f"{cfg.get('db_type')}:{cfg.get('host')}:{cfg.get('port')}:{cfg.get('name')}"
    except Exception:
        return None
    try:
        from security.context import get_acl
        acl = get_acl()
        ident = ""
        if acl is not None:
            ident = str(getattr(acl, "username", "") or "")
            if not ident:
                roles = getattr(acl, "roles", None) or []
                ident = ",".join(sorted(str(r) for r in roles))
            # 行级/列级约束也要进键，否则同角色不同授权的用户仍会互窜
            try:
                ident += f"|rf={bool(getattr(acl, 'row_filters', None))}"
                ident += f"|cd={bool(getattr(acl, 'column_denies', None))}"
                ident += f"|cm={bool(getattr(acl, 'column_masks', None))}"
            except Exception:
                pass
        # 拿不到身份（未登录 guest）时给一个显式标记，避免与已登录用户共用同一条缓存
        return f"{base}|u={ident or 'anonymous'}"
    except Exception:
        # 取不到 ACL 上下文时宁可不缓存，也不要冒跨用户泄漏的风险
        return None


def _fmt(v: float) -> str:
    """数值 → 中文友好展示（万/亿缩写）。"""
    if v is None:
        return "0"
    if abs(v) >= 100000000:
        return f"{v / 100000000:.1f}亿"
    if abs(v) >= 10000:
        return f"{v / 10000:.1f}万"
    if abs(v - round(v)) < 1e-6:
        return f"{int(v):,}"
    return f"{v:,.1f}"


def _clean_name(name: str) -> str | None:
    s = str(name).strip()
    if not s or s.lower() in ("none", "null", ""):
        return None
    return s


def _round2(v: float) -> float:
    return round(float(v), 2)


# ── 字段语义探测 ──────────────────────────────────────

def _field_names(fields: list[dict]) -> list[str]:
    return [str(f.get("name") or "").lower() for f in fields]


def _find_field(fields: list[dict], *subs: str) -> str | None:
    """返回第一个字段名（原始大小写）——其 name 包含任一 subs 子串。"""
    for f in fields:
        name = str(f.get("name") or "").lower()
        if any(s in name for s in subs):
            return f.get("name")
    return None


def _has_field(fields: list[dict], *subs: str) -> bool:
    return _find_field(fields, *subs) is not None


# 常见字段名 → 中文兜底（数据库字段无注释时，保证标签可读）
_FIELD_CN: dict[str, str] = {
    "product_id": "产品", "product_code": "产品", "product_name": "产品", "product": "产品",
    "category": "分类", "spec": "规格", "unit": "单位", "name": "名称",
    "warehouse_code": "仓库", "warehouse": "仓库", "line_id": "产线", "line_name": "产线",
    "process_id": "工序", "process_name": "工序", "process_seq": "工序顺序",
    "work_order_id": "工单", "shift_code": "班次", "shift": "班次",
    "defect_type": "缺陷类型", "defect_quantity": "不良数", "severity": "严重程度",
    "disposal": "处置方式", "status": "状态", "type": "类型", "reason": "原因",
    "result": "检验结果", "inspection_result": "检验结果",
    "input_qty": "投入量", "output_qty": "产出量", "good_qty": "合格产量",
    "produced_quantity": "产量", "qualified_quantity": "合格量",
    "defect_qty": "不良数", "available_qty": "可用库存", "frozen_qty": "冻结库存",
    "safety_stock_qty": "安全库存", "safety_stock": "安全库存", "stock_qty": "库存量",
    "available": "可用量", "quantity": "数量", "amount": "金额",
    "downtime_minutes": "停机时长", "downtime_duration": "停机时长",
    "downtime_reason": "停机原因", "downtime_start": "停机开始", "downtime_end": "停机结束",
    "plan_qty": "计划数", "actual_qty": "实际数",
    "planned_quantity": "计划数", "actual_quantity": "实际数",
    "equipment_id": "设备", "equipment_name": "设备",
    "material_id": "物料", "material_name": "物料", "material_code": "物料编码",
    "operator_name": "操作员", "operator": "操作员", "inspector": "检验员",
    "customer": "客户", "customer_name": "客户", "supplier": "供应商", "supplier_name": "供应商",
    "factory": "工厂", "department": "部门", "dept": "部门", "employee": "员工",
    "sales": "销售", "price": "价格", "order_id": "订单", "order": "订单",
    "production_line": "产线", "production_date": "生产日期",
    "inspection_date": "检验日期", "stat_date": "日期", "snapshot_date": "日期",
    "start_time": "开始时间", "end_time": "结束时间",
    "created_at": "创建时间", "updated_at": "更新时间", "last_updated": "更新时间",
    "date": "日期", "time": "时间", "qty": "数量", "count": "次数",
}


def _field_label(f: dict, fallback: str) -> str:
    """字段显示名：优先 DB 中文描述 → 常见字段名兜底映射 → 原始字段名。"""
    name = str(f.get("name") or "")
    try:
        from agent.suggest import _friendly_label
        lab = _friendly_label(f)
        if lab and lab != name:
            return lab
    except Exception:
        pass
    return _FIELD_CN.get(name.lower()) or fallback


def _table_display(table: str) -> str:
    return table.split(".")[-1]


def _tlabel(tmeta: dict, table: str) -> str:
    """表显示名：优先中文别名；无中文别名（如 factory.attendance 的裸表名别名）
    时去掉 schema 前缀，避免标题出现「factory.attendance」这种带点的原始名。"""
    alias = str(tmeta.get("table_alias") or "").strip()
    if alias and re.search(r"[\u4e00-\u9fff]", alias):
        return alias
    return _table_display(table)


def _field_by_name(fields: list[dict], name: str) -> dict:
    for f in fields:
        if str(f.get("name") or "") == name:
            return f
    return {}


# 严重程度 → 中文
_SEVERITY_CN: dict[str, str] = {
    "critical": "致命", "major": "重大", "minor": "轻微",
    "high": "高", "medium": "中", "low": "低",
    "致命": "致命", "重大": "重大", "轻微": "轻微",
}


def _sev_cn(v: str) -> str:
    return _SEVERITY_CN.get(str(v).strip().lower(), _clean_name(v) or str(v))


# ── 解释文案（白话，确定性规则） ──────────────────────

def _interpret_gauge(value: float, good: float, inp: float, grade: str) -> str:
    return (f"全厂综合良率为 {value:.2f}%：共投入 {_fmt(inp)} 件，合格 {_fmt(good)} 件、"
            f"不合格 {_fmt(inp - good)} 件。当前处于「{grade}」水平"
            f"（优秀≥98%、良好 95%~98%、需关注<95%）。")


def _interpret_funnel(data: list[dict], metric_label: str, dim_label: str) -> str:
    first, last = data[0], data[-1]
    loss = first["value"] - last["value"]
    rate = loss / first["value"] * 100 if first["value"] else 0
    # 找损耗最大的「落差段」
    max_step, step_from, step_to = 0, "", ""
    for i in range(len(data) - 1):
        gap = data[i]["value"] - data[i + 1]["value"]
        if gap > max_step:
            max_step, step_from, step_to = gap, data[i]["name"], data[i + 1]["name"]
    tail = (f"损耗最大的环节是「{step_from}」→「{step_to}」（减少 {_fmt(max_step)} 件），"
            f"建议优先排查该环节的良率。") if max_step > 0 else "各环节损耗较为均匀。"
    return (f"按{dim_label}看「{metric_label}」，从「{first['name']}」的 {_fmt(first['value'])} "
            f"逐级递减到「{last['name']}」的 {_fmt(last['value'])}，累计损耗 {_fmt(loss)} 件"
            f"（约 {rate:.0f}%）。{tail}")


def _interpret_sunburst(total: int, top_type: str, top_n: int, top_sev: str, top_sev_n: int) -> str:
    return (f"缺陷共 {total} 起，其中「{top_type}」最多（{top_n} 起，占 {top_n / total * 100:.0f}%），"
            f"是首要质量问题；从严重度看「{top_sev}」最突出（{top_sev_n} 起）。"
            f"建议优先围绕「{top_type}」做根因分析。")


def _interpret_pareto(data: list[dict], total: float, top_k: int, top_share: float) -> str:
    top_names = "、".join(x["name"] for x in data[:top_k])
    return (f"累计 {_fmt(total)}，共 {len(data)} 类原因。按「二八法则」，前 {top_k} 项"
            f"（{top_names}）合计占 {top_share:.0f}%，是主要来源，优先处理即可覆盖绝大部分问题。")


def _interpret_stock(alert_names: list[str], healthy: int, total: int) -> str:
    if alert_names:
        return (f"共 {total} 个仓库，其中 {healthy} 个库存充足、"
                f"{len(alert_names)} 个低于安全库存：{('、'.join(alert_names))}，需及时补货。")
    return f"共 {total} 个仓库，库存均达到或超过安全线，供应充足。"


def _interpret_stacked(cats: list[str], good_series: list[float], defect_series: list[float]) -> str:
    rates = [(c, g / (g + d) * 100 if (g + d) else 0) for c, g, d in zip(cats, good_series, defect_series)]
    best = max(rates, key=lambda x: x[1])
    worst = min(rates, key=lambda x: x[1])
    total_defect = sum(defect_series)
    return (f"各{'' if len(cats) <= 1 else '维度'}良品率均在 {worst[1]:.0f}% 以上，"
            f"「{best[0]}」最高（{best[1]:.1f}%）、「{worst[0]}」最低（{worst[1]:.1f}%）；"
            f"不良合计 {_fmt(total_defect)} 件，是质量提升的重点。")


# ── 创意图表数据生成 ─────────────────────────────────

def _build_gauge(table: str, tlab: str, fields: list[dict]) -> dict | None:
    """综合良率仪表盘：需要 good_qty 与 input_qty（或 output_qty）。"""
    good_col = _find_field(fields, "good_qty", "qualified_quantity", "qualified_qty")
    inp_col = _find_field(fields, "input_qty", "output_qty", "produced_quantity")
    if not good_col or not inp_col:
        return None
    sql = (f"SELECT SUM({_q(good_col)}) AS g, SUM({_q(inp_col)}) AS i FROM {_q_table(table)}")
    r = _safe_execute(sql)
    if not r.get("success") or not r.get("rows"):
        return None
    row = r["rows"][0]
    g, i = _to_num(row.get("g")), _to_num(row.get("i"))
    if not i or i <= 0 or g is None:
        return None
    value = _round2(g / i * 100)
    grade = "优秀" if value >= 98 else ("良好" if value >= 95 else "需关注")
    return {
        "id": f"gauge:{table}:yield",
        "type": "gauge",
        "title": "全厂综合良率",
        "table": table,
        "table_label": tlab,
        "metric_label": "综合良率",
        "dim_label": "",
        "data": {"value": value, "unit": "%", "min": 0, "max": 100, "grade": grade},
        "interpretation": _interpret_gauge(value, g, i, grade),
        "sql": sql,
        "used_fields": [
            {"name": good_col, "label": _field_label(_field_by_name(fields, good_col), good_col), "role": "度量"},
            {"name": inp_col, "label": _field_label(_field_by_name(fields, inp_col), inp_col), "role": "度量"},
        ],
    }


def _build_funnel(table: str, tlab: str, fields: list[dict]) -> dict | None:
    """工序损耗漏斗：有 process_id + 数量字段，按工序分组投入量。"""
    dim = _find_field(fields, "process_id", "process_name")
    if not dim:
        return None
    measure = _find_field(fields, "input_qty", "output_qty", "good_qty", "quantity", "amount")
    if not measure:
        return None
    d_lab = _field_label(_field_by_name(fields, dim), dim)
    m_lab = _field_label(_field_by_name(fields, measure), measure)
    sql = (f"SELECT {_q(dim)} AS d, SUM({_q(measure)}) AS v "
           f"FROM {_q_table(table)} WHERE {_q(dim)} IS NOT NULL "
           f"GROUP BY {_q(dim)} ORDER BY {_q(dim)}")
    r = _safe_execute(sql)
    data = []
    if r.get("success"):
        for row in r.get("rows") or []:
            v = _to_num(row.get("v"))
            name = _clean_name(row.get("d"))
            if v is not None and name:
                data.append({"name": name, "value": v})
    # 漏斗图要求逐级递减形态，这里按值降序保证形态（若工序序恰好递减则与工序序一致）
    data.sort(key=lambda x: x["value"], reverse=True)
    if len(data) < 3:
        return None
    return {
        "id": f"funnel:{table}:{dim}",
        "type": "funnel",
        "title": f"各{d_lab}的{m_lab}损耗漏斗",
        "table": table,
        "table_label": tlab,
        "metric_label": m_lab,
        "dim_label": d_lab,
        "data": data,
        "interpretation": _interpret_funnel(data, m_lab, d_lab),
        "sql": sql,
        "used_fields": [
            {"name": dim, "label": d_lab, "role": "维度"},
            {"name": measure, "label": m_lab, "role": "度量"},
        ],
    }


def _build_sunburst(table: str, tlab: str, fields: list[dict]) -> dict | None:
    """缺陷构成旭日图：severity → defect_type 两级层级。"""
    sev_col = _find_field(fields, "severity")
    type_col = _find_field(fields, "defect_type", "defect_reason", "type")
    if not sev_col or not type_col:
        return None
    sql = (f"SELECT {_q(sev_col)} AS s, {_q(type_col)} AS t, COUNT(*) AS n "
           f"FROM {_q_table(table)} WHERE {_q(sev_col)} IS NOT NULL AND {_q(type_col)} IS NOT NULL "
           f"GROUP BY {_q(sev_col)}, {_q(type_col)} ORDER BY n DESC")
    r = _safe_execute(sql)
    rows = r.get("rows") if r.get("success") else []
    if not rows:
        return None
    # 汇总按严重度聚合（用于找 top 严重度）
    sev_total: dict[str, int] = {}
    type_total: dict[str, int] = {}
    for row in rows:
        s = _sev_cn(row.get("s"))
        t = _clean_name(row.get("t"))
        n = int(_to_num(row.get("n")) or 0)
        if not t:
            continue
        sev_total[s] = sev_total.get(s, 0) + n
        type_total[t] = type_total.get(t, 0) + n
    # 构建旭日层级：严重度（按总数降序）→ 类型
    children: list[dict] = []
    for s, s_n in sorted(sev_total.items(), key=lambda x: -x[1]):
        kids = [{"name": _clean_name(row.get("t")), "value": int(_to_num(row.get("n")) or 0)}
                for row in rows if _sev_cn(row.get("s")) == s and _clean_name(row.get("t"))]
        children.append({"name": s, "children": kids})
    # 2026-10-03 修复：type_total / sev_total 只在有名字的行才写入，若所有行的类型
    # （或严重度）都为空串会被 continue 跳过 → 两个 dict 为空 → max() 抛
    # ValueError: max() arg is an empty sequence（再被上层 except 吞成旭日图静默消失）。
    if not type_total or not sev_total:
        return None
    total = sum(type_total.values())
    if not total:
        return None
    top_type = max(type_total, key=type_total.get)
    top_sev = max(sev_total, key=sev_total.get)
    return {
        "id": f"sunburst:{table}:{sev_col}:{type_col}",
        "type": "sunburst",
        "title": "缺陷构成（严重度 → 类型）",
        "table": table,
        "table_label": tlab,
        "metric_label": "缺陷数量",
        "dim_label": "严重度/类型",
        "data": children,
        "interpretation": _interpret_sunburst(total, top_type, type_total[top_type],
                                              top_sev, sev_total[top_sev]),
        "sql": sql,
        "used_fields": [
            {"name": sev_col, "label": _field_label(_field_by_name(fields, sev_col), sev_col), "role": "维度"},
            {"name": type_col, "label": _field_label(_field_by_name(fields, type_col), type_col), "role": "维度"},
            {"name": "*", "label": "记录数", "role": "度量"},
        ],
    }


def _build_pareto(table: str, tlab: str, fields: list[dict]) -> dict | None:
    """停机/原因帕累托：reason + 时长字段。"""
    reason_col = _find_field(fields, "reason")
    duration_col = _find_field(fields, "downtime_minutes", "duration", "minutes", "qty", "quantity")
    if not reason_col or not duration_col:
        return None
    sql = (f"SELECT {_q(reason_col)} AS d, SUM({_q(duration_col)}) AS v "
           f"FROM {_q_table(table)} WHERE {_q(reason_col)} IS NOT NULL "
           f"GROUP BY {_q(reason_col)} ORDER BY v DESC")
    r = _safe_execute(sql)
    data = []
    if r.get("success"):
        for row in r.get("rows") or []:
            v = _to_num(row.get("v"))
            name = _clean_name(row.get("d"))
            if v is not None and name:
                data.append({"name": name, "value": v})
    if len(data) < 2:
        return None
    total = sum(x["value"] for x in data)
    top_k, cum_top = 0, 0.0
    for i, x in enumerate(data, 1):
        cum_top += x["value"]
        # 累计占比首次达到约 70%（round 到 3 位，避免 69.96% 这种边界被误判）
        if round(cum_top / total, 3) >= 0.7:
            top_k = i
            break
    if top_k == 0:
        top_k = len(data)
        cum_top = total
    return {
        "id": f"pareto:{table}:{reason_col}",
        "type": "pareto",
        "title": f"「{_field_label(_field_by_name(fields, reason_col), reason_col)}」帕累托分析",
        "table": table,
        "table_label": tlab,
        "metric_label": _field_label(_field_by_name(fields, duration_col), duration_col),
        "dim_label": _field_label(_field_by_name(fields, reason_col), reason_col),
        "data": data,
        "interpretation": _interpret_pareto(data, total, top_k, round(cum_top / total * 100)),
        "sql": sql,
        "used_fields": [
            {"name": reason_col, "label": _field_label(_field_by_name(fields, reason_col), reason_col), "role": "维度"},
            {"name": duration_col, "label": _field_label(_field_by_name(fields, duration_col), duration_col), "role": "度量"},
        ],
    }


def _build_stock_health(table: str, tlab: str, fields: list[dict]) -> dict | None:
    """库存健康度：可用库存 vs 安全库存，低于安全线的仓库标红。"""
    avail_col = _find_field(fields, "available_qty", "available", "stock_qty", "quantity")
    safe_col = _find_field(fields, "safety_stock_qty", "safety_qty", "safety")
    dim = _find_field(fields, "warehouse_code", "warehouse", "location")
    if not avail_col or not safe_col or not dim:
        return None
    sql = (f"SELECT {_q(dim)} AS d, SUM({_q(avail_col)}) AS a, SUM({_q(safe_col)}) AS s "
           f"FROM {_q_table(table)} WHERE {_q(dim)} IS NOT NULL "
           f"GROUP BY {_q(dim)} ORDER BY d")
    r = _safe_execute(sql)
    data = []
    if r.get("success"):
        for row in r.get("rows") or []:
            name = _clean_name(row.get("d"))
            a = _to_num(row.get("a")) or 0
            s = _to_num(row.get("s")) or 0
            if not name:
                continue
            # 2026-10-01 修复：安全库存合计为 0 时无"达成率"可言，此前用 999.0 哨兵
            # 会被前端直接展示成「达成率：999%」。改为 null，前端显示「—」。
            ratio = _round2(a / s * 100) if s > 0 else None
            data.append({"name": name, "value": a, "safe": s, "ratio": ratio,
                         "alert": s > 0 and a < s})
    if len(data) < 2:
        return None
    alert_names = [x["name"] for x in data if x.get("alert")]
    d_lab = _field_label(_field_by_name(fields, dim), dim)
    return {
        "id": f"stock:{table}:{dim}",
        "type": "stock-health",
        "title": f"各{d_lab}库存健康度",
        "table": table,
        "table_label": tlab,
        "metric_label": "可用库存",
        "dim_label": d_lab,
        "data": data,
        "interpretation": _interpret_stock(alert_names, len(data) - len(alert_names), len(data)),
        "sql": sql,
        "used_fields": [
            {"name": dim, "label": d_lab, "role": "维度"},
            {"name": avail_col, "label": _field_label(_field_by_name(fields, avail_col), avail_col), "role": "度量"},
            {"name": safe_col, "label": _field_label(_field_by_name(fields, safe_col), safe_col), "role": "度量"},
        ],
    }


def _build_stacked(table: str, tlab: str, fields: list[dict]) -> dict | None:
    """合格/不良堆叠构成：good_qty + defect_qty + 维度。"""
    good_col = _find_field(fields, "good_qty", "qualified_quantity", "qualified_qty")
    defect_col = _find_field(fields, "defect_qty", "defect_quantity")
    if not good_col or not defect_col:
        return None
    dim = _find_field(fields, "process_id", "line_id", "category", "product_name")
    if not dim:
        return None
    d_lab = _field_label(_field_by_name(fields, dim), dim)
    sql = (f"SELECT {_q(dim)} AS d, SUM({_q(good_col)}) AS g, SUM({_q(defect_col)}) AS b "
           f"FROM {_q_table(table)} WHERE {_q(dim)} IS NOT NULL "
           f"GROUP BY {_q(dim)} ORDER BY g DESC")
    r = _safe_execute(sql)
    cats, goods, defects = [], [], []
    if r.get("success"):
        for row in r.get("rows") or []:
            name = _clean_name(row.get("d"))
            g = _to_num(row.get("g"))
            b = _to_num(row.get("b"))
            if not name or g is None:
                continue
            cats.append(name)
            goods.append(g)
            defects.append(b or 0)
    if len(cats) < 2:
        return None
    return {
        "id": f"stacked:{table}:{dim}",
        "type": "stacked-bar",
        "title": f"各{d_lab}合格 / 不良构成",
        "table": table,
        "table_label": tlab,
        "metric_label": "产量",
        "dim_label": d_lab,
        "data": {"categories": cats, "series": [
            {"name": "合格", "data": goods},
            {"name": "不良", "data": defects},
        ]},
        "interpretation": _interpret_stacked(cats, goods, defects),
        "sql": sql,
        "used_fields": [
            {"name": dim, "label": d_lab, "role": "维度"},
            {"name": good_col, "label": _field_label(_field_by_name(fields, good_col), good_col), "role": "度量"},
            {"name": defect_col, "label": _field_label(_field_by_name(fields, defect_col), defect_col), "role": "度量"},
        ],
    }


# ════════════════════════════════════════════════════════════════════
# 通用层：任意数据库「重点问题」驱动 + 数据形态驱动创意图表
# ════════════════════════════════════════════════════════════════════
# 对齐用户三条诉求：
# 1) 动态适配当前库：按字段类型通用分类（度量/维度/时间），不依赖硬编码业务字段名；
# 2) 类型多样有设计：line/area/radar/heatmap/scatter/rose/donut/barh/sunburst/funnel，
#    由数据形态 + 洞察语义确定性选型，绝不全是一种柱状图；
# 3) 重点问题驱动：复用 insight_scan 自动发现离群/失衡/趋势突变等异常，
#    把「值得看的重点问题」直接做成图表，附确定性白话解读。
#
# 返回结构（前端 DataChartCard 走统一 renderChart 引擎渲染）：
#   {id, type, title, table, table_label, metric_label, dim_label,
#    columns, rows, interpretation, insight_type, severity}
# ════════════════════════════════════════════════════════════════════

_GENERIC_MIN_ROWS = 5   # 表行数少于此不做通用图（样本不足，结论不可靠）


def _g_exec(sql: str):
    """安全执行一条只读聚合 SQL，失败返回 None。"""
    r = _safe_execute(sql)
    if not r or not r.get("success"):
        return None
    return r


def _g_friendly(field: dict) -> str:
    return _field_label(field, str(field.get("name") or ""))


def _g_dim_measure(table, tlab, dim_f, measure_f, limit=12, order="DESC"):
    """单维度 × 单度量聚合。返回 {d_lab, m_lab, columns, rows, vals} 或 None。"""
    d, m = dim_f["name"], measure_f["name"]
    d_lab, m_lab = _g_friendly(dim_f), _g_friendly(measure_f)
    sql = (f"SELECT {_q(d)} AS {_q(d_lab)}, SUM({_q(m)}) AS {_q(m_lab)} "
           f"FROM {_q_table(table)} WHERE {_q(d)} IS NOT NULL "
           f"GROUP BY {_q(d)} ORDER BY SUM({_q(m)}) {order} LIMIT {limit}")
    r = _g_exec(sql)
    if not r:
        return None
    rows = [row for row in (r.get("rows") or []) if _clean_name(row.get(d_lab))]
    if len(rows) < 2:
        return None
    vals = [(_to_num(row.get(m_lab)) or 0.0) for row in rows]
    return {"d_lab": d_lab, "m_lab": m_lab, "columns": [d_lab, m_lab],
            "rows": rows, "vals": vals, "sql": sql,
            "used_fields": [{"name": d, "label": d_lab, "role": "维度"},
                            {"name": m, "label": m_lab, "role": "度量"}]}


def _g_trend(table, tlab, time_f, measure_f, limit=30):
    """时间 × 度量趋势。返回 {t_lab, m_lab, columns, rows} 或 None。

    时间列若是 timestamp/datetime 类型，按「日」截断聚合，避免按精确时刻分组
    得到大量无意义的单点（趋势必须是连续的时间轴）。
    """
    t, m = time_f["name"], measure_f["name"]
    t_lab, m_lab = _g_friendly(time_f), _g_friendly(measure_f)
    t_type = _norm_type(str(time_f.get("type") or "").lower().strip())
    is_ts = ("timestamp" in t_type) or t_type in ("datetime", "time")
    if is_ts:
        try:
            from database import get_db_type
            if get_db_type() == "mysql":
                texpr = f"DATE({_q(t)})"
            else:
                texpr = f"to_char({_q(t)}, 'YYYY-MM-DD')"
        except Exception:
            texpr = _q(t)
    else:
        texpr = _q(t)
    # 数据正确性修复（P0）：原为 ORDER BY 时间 ASC LIMIT n —— 取到的是**最早** n 期，
    # 而趋势图/分析语义是"最近走势"。改为倒序取最新 n 期后反转回时间正序。
    sql = (f"SELECT {texpr} AS {_q(t_lab)}, SUM({_q(m)}) AS {_q(m_lab)} "
           f"FROM {_q_table(table)} WHERE {_q(t)} IS NOT NULL "
           f"GROUP BY {texpr} ORDER BY {texpr} DESC LIMIT {limit}")
    r = _g_exec(sql)
    if not r:
        return None
    rows = list(reversed(r.get("rows") or []))   # 倒序 → 时间正序
    if len(rows) < 3:
        return None
    return {"t_lab": t_lab, "m_lab": m_lab, "columns": [t_lab, m_lab], "rows": rows,
            "sql": sql, "used_fields": [{"name": t, "label": t_lab, "role": "时间"},
                                        {"name": m, "label": m_lab, "role": "度量"}]}


def _g_radar(table, tlab, dim_f, measure_fs):
    """1 维度 × 2~4 度量 → 雷达图。"""
    d = dim_f["name"]
    d_lab = _g_friendly(dim_f)
    parts = [f"{_q(d)} AS {_q(d_lab)}"]
    labs = []
    for mf in measure_fs:
        ml = _g_friendly(mf)
        parts.append(f"SUM({_q(mf['name'])}) AS {_q(ml)}")
        labs.append(ml)
    sql = (f"SELECT {', '.join(parts)} FROM {_q_table(table)} WHERE {_q(d)} IS NOT NULL "
           f"GROUP BY {_q(d)} ORDER BY 1 LIMIT 12")
    r = _g_exec(sql)
    if not r:
        return None
    rows = r.get("rows") or []
    if len(rows) < 3:
        return None
    fields_used = [{"name": d, "label": d_lab, "role": "维度"}] + \
                  [{"name": mf["name"], "label": _g_friendly(mf), "role": "度量"} for mf in measure_fs]
    return {"d_lab": d_lab, "m_labs": labs, "columns": [d_lab] + labs, "rows": rows,
            "sql": sql, "used_fields": fields_used}


def _g_heatmap(table, tlab, d1, d2, measure_f, limit=200):
    """2 维度 × 1 度量 → 热力图。"""
    d1_lab, d2_lab = _g_friendly(d1), _g_friendly(d2)
    m_lab = _g_friendly(measure_f)
    sql = (f"SELECT {_q(d1['name'])} AS {_q(d1_lab)}, {_q(d2['name'])} AS {_q(d2_lab)}, "
           f"SUM({_q(measure_f['name'])}) AS {_q(m_lab)} "
           f"FROM {_q_table(table)} WHERE {_q(d1['name'])} IS NOT NULL AND {_q(d2['name'])} IS NOT NULL "
           f"GROUP BY {_q(d1['name'])}, {_q(d2['name'])} LIMIT {limit}")
    r = _g_exec(sql)
    if not r:
        return None
    rows = r.get("rows") or []
    if len(rows) < 6:
        return None
    return {"columns": [d1_lab, d2_lab, m_lab], "rows": rows, "sql": sql,
            "used_fields": [{"name": d1["name"], "label": d1_lab, "role": "维度"},
                            {"name": d2["name"], "label": d2_lab, "role": "维度"},
                            {"name": measure_f["name"], "label": m_lab, "role": "度量"}]}


def _g_scatter(table, tlab, m1, m2, limit=100):
    """2 数值 → 散点（原始行，不做聚合）。"""
    m1_lab, m2_lab = _g_friendly(m1), _g_friendly(m2)
    sql = (f"SELECT {_q(m1['name'])} AS {_q(m1_lab)}, {_q(m2['name'])} AS {_q(m2_lab)} "
           f"FROM {_q_table(table)} WHERE {_q(m1['name'])} IS NOT NULL AND {_q(m2['name'])} IS NOT NULL "
           f"LIMIT {limit}")
    r = _g_exec(sql)
    if not r:
        return None
    rows = r.get("rows") or []
    if len(rows) < 3:
        return None
    return {"columns": [m1_lab, m2_lab], "rows": rows, "sql": sql,
            "used_fields": [{"name": m1["name"], "label": m1_lab, "role": "度量"},
                            {"name": m2["name"], "label": m2_lab, "role": "度量"}]}


def _pick_rank_type(rows, vals, d_lab):
    """单维单度量选型：构成集中→donut；适中构成→rose；长标签/多类→barh；否则 bar。"""
    n = len(rows)
    non_neg = all(v >= 0 for v in vals)
    if non_neg and 3 <= n <= 12:
        total = sum(vals)
        if total and max(vals) / total >= 0.6:
            return "donut"
        return "rose"
    lens = [len(str(row.get(d_lab) or "")) for row in rows]
    avg = sum(lens) / n if lens else 0
    if avg > 6 or n > 12:
        return "barh"
    return "bar"


# ── 通用白话解读（确定性规则）─────────────────────────────

def _interpret_generic_trend(rows, t_lab, m_lab):
    vals = [(_to_num(r.get(m_lab)) or 0.0) for r in rows]
    if len(vals) < 2:
        return f"「{m_lab}」随时间变化，共 {len(vals)} 个时间点。"
    first, last = vals[0], vals[-1]
    chg = (last - first) / abs(first) * 100 if first else None
    peak_i = max(range(len(vals)), key=lambda i: vals[i])
    direction = "上升" if last > first else ("下降" if last < first else "持平")
    chg_s = f"，变化 {chg:+.0f}%" if chg is not None else ""
    return (f"「{m_lab}」整体{direction}（{_fmt(first)} → {_fmt(last)}{chg_s}），"
            f"峰值出现在「{rows[peak_i].get(t_lab)}」（{_fmt(vals[peak_i])}）。")


def _interpret_heatmap(rows):
    if not rows:
        return "二维交叉分布。"
    keys = [k for k in rows[0].keys()]
    val_col = keys[-1]
    best = max(rows, key=lambda r: (_to_num(r.get(val_col)) or 0.0))
    return (f"共 {len(rows)} 个交叉组合，「{best.get(keys[0])} × {best.get(keys[1])}」"
            f"的{val_col}最高（{_fmt(_to_num(best.get(val_col)) or 0)}）。")


def _interpret_radar(rows, d_lab, m_labs):
    notes = []
    for ml in m_labs[:3]:
        best = max(rows, key=lambda r: (_to_num(r.get(ml)) or 0.0))
        notes.append(f"{ml}最高为「{best.get(d_lab)}」")
    return "；".join(notes) + "，可据此定位各维度优势项。"


def _interpret_scatter(rows, m1, m2):
    if len(rows) < 3:
        return f"「{m1}」与「{m2}」散点分布。"
    xs = [(_to_num(r.get(m1)) or 0.0) for r in rows]
    ys = [(_to_num(r.get(m2)) or 0.0) for r in rows]
    try:
        import statistics as _st
        mx, my = _st.fmean(xs), _st.fmean(ys)
        sx = _st.pstdev(xs) if len(xs) > 1 else 0.0
        sy = _st.pstdev(ys) if len(ys) > 1 else 0.0
        if sx and sy:
            cov = _st.fmean([(x - mx) * (y - my) for x, y in zip(xs, ys)])
            rr = max(-1.0, min(1.0, cov / (sx * sy)))
            rel = "正相关" if rr > 0.5 else ("负相关" if rr < -0.5 else "相关性较弱")
            return f"「{m1}」与「{m2}」{rel}（Pearson r={rr:.2f}），样本 {len(rows)} 条。"
    except Exception:
        pass
    return f"「{m1}」与「{m2}」散点分布，共 {len(rows)} 条记录。"


def _interpret_rank(rows, d_lab, m_lab, ctype):
    if not rows:
        return f"各{d_lab}的{m_lab}对比。"
    vals = [(_to_num(r.get(m_lab)) or 0.0) for r in rows]
    total = sum(vals)
    top = rows[0]
    top_v = vals[0]
    if ctype in ("donut", "rose") and total:
        share = top_v / total * 100
        return (f"共 {len(rows)} 个{d_lab}，「{top.get(d_lab)}」的{m_lab}最高"
                f"（{_fmt(top_v)}，占 {share:.0f}%），是当前主要构成。")
    last = rows[-1]
    return (f"「{top.get(d_lab)}」的{m_lab}最高（{_fmt(top_v)}），"
            f"「{last.get(d_lab)}」最低（{_fmt(vals[-1])}），两者相差 {_fmt(top_v - vals[-1])}。")


# ── 洞察 → 图表（重点问题驱动）──────────────────────────

def _attach_lineage(chart: dict, table: str, tlab: str) -> dict:
    """给图表附加溯源信息：来源表（schema.table 全名 + 中文名）+ 用到的字段。

    面向业务人员的展示元数据，不含 SQL；内部辅助键（sql/used_fields/d_lab/…）不下发前端。
    """
    chart["lineage"] = {
        "table": table,
        "table_label": tlab,
        "fields": chart.get("used_fields") or [],
    }
    for k in ("sql", "used_fields", "d_lab", "m_lab", "t_lab", "m_labs", "vals"):
        chart.pop(k, None)
    return chart


def _problem_to_chart(table, tmeta, fields, it):
    """把 insight_scan 发现的一条「重点问题」转成图表。"""
    itype = it.get("type")
    metric = it.get("metric")
    dim = it.get("dim")
    if not metric or not dim:
        return None
    tlab = _tlabel(tmeta, table)
    mf = _field_by_name(fields, metric) or {"name": metric, "description": it.get("metric_label") or metric}
    df = _field_by_name(fields, dim) or {"name": dim, "description": it.get("dim_label") or dim}
    m_lab = _g_friendly(mf)
    d_lab = _g_friendly(df)
    base = {"table": table, "table_label": tlab, "metric_label": m_lab,
            "dim_label": d_lab, "insight_type": itype, "severity": it.get("severity", 1.0)}
    # 趋势突变 → 折线 / 面积
    if itype == "trend_break":
        c = _g_trend(table, tlab, df, mf)
        if c:
            dev = _to_num(it.get("deviation_pct") or 0) or 0
            c.update(base)
            c["type"] = "area" if dev < 0 else "line"
            c["title"] = f"{tlab}·{m_lab}趋势异动"
            c["id"] = f"generic:{itype}:{table}:{metric}"
            c["interpretation"] = it.get("message") or \
                _interpret_generic_trend(c["rows"], c["t_lab"], c["m_lab"])
            return c
    # 离群 / 失衡 → 维度对比（离群→barh 突出异常；失衡→donut 突出集中）
    if itype in ("outlier", "imbalance"):
        c = _g_dim_measure(table, tlab, df, mf)
        if c:
            c.update(base)
            c["type"] = "barh" if itype == "outlier" else "donut"
            c["title"] = f"{tlab}·{d_lab}的{m_lab}{'分布' if itype == 'imbalance' else '对比'}"
            c["id"] = f"generic:{itype}:{table}:{metric}"
            c["interpretation"] = it.get("message") or \
                _interpret_rank(c["rows"], c["d_lab"], c["m_lab"], c["type"])
            return c
    return None


def _dim_score(f) -> int:
    """维度质量评分（越小越适合做维度）：业务维度 < 名称/编码 < 纯外键。

    用于避免热力图选到「员工编码 × 员工名称」这种 1:1 低价值组合，以及
    「id × 外键」这类纯标识维度。
    """
    name = str(f.get("name") or "").lower()
    if name.endswith("_id") or name == "id":
        return 3
    if name.endswith("_code") or name == "code" or name.endswith("_no") or name == "no":
        return 2
    if any(w in name for w in ("name", "status", "type", "category", "shift",
                               "warehouse", "reason", "grade", "level", "state", "result")):
        return 0
    return 1


def _pick_distinct_dims(dims):
    """挑两个「显示名不同」的维度，避免同义交叉（工序×工序）与低价值组合（ID×编码）。"""
    seen: set[str] = set()
    ordered = sorted(dims, key=_dim_score)
    pair: list[dict] = []
    for d in ordered:
        lab = _g_friendly(d)
        if lab in seen:
            continue
        # 已有成员时，拒绝两个都是 ID 类（score>=2）的纯编码组合
        if pair and _dim_score(d) >= 2 and _dim_score(pair[0]) >= 2:
            continue
        seen.add(lab)
        pair.append(d)
        if len(pair) == 2:
            return pair
    return pair


# ── 类型多样性预算：一批图表中同一形态最多出现 N 次，避免清一色折线/柱状 ──
_MAX_SAME_TYPE = 2

_TREND_TYPES = ("line", "area")


def _g_type_group(t: str) -> str:
    """图表类型 → 形态组：line/area 归为趋势组，其余按类型本身。"""
    return "trend" if t in _TREND_TYPES else str(t or "none")


def _budget_ok(budget: dict, group: str) -> bool:
    return budget.get(group, 0) < _MAX_SAME_TYPE


def _budget_take(budget: dict, group: str) -> None:
    budget[group] = budget.get(group, 0) + 1


def _shape_to_chart(table, tmeta, fields, measures, dims, times, type_budget: dict | None = None):
    """按数据形态确定性选型并生成一张图（通用、不依赖业务字段）。

    type_budget 记录各形态组已用次数，超限的形态跳过，保证一批图表类型多样。
    """
    type_budget = type_budget or {}
    tlab = _tlabel(tmeta, table)
    if times and measures and _budget_ok(type_budget, "trend"):
        c = _g_trend(table, tlab, times[0], measures[0])
        if c:
            c.update({"type": "line", "title": f"{tlab}·{c['m_lab']}趋势",
                      "table": table, "table_label": tlab,
                      "metric_label": c["m_lab"], "dim_label": c["t_lab"],
                      "interpretation": _interpret_generic_trend(c["rows"], c["t_lab"], c["m_lab"]),
                      "insight_type": "shape", "severity": 0.5})
            _budget_take(type_budget, "trend")
            return c
    dim_pair = _pick_distinct_dims(dims)
    if len(dim_pair) == 2 and len(measures) >= 1 and _budget_ok(type_budget, "heatmap"):
        c = _g_heatmap(table, tlab, dim_pair[0], dim_pair[1], measures[0])
        if c:
            c.update({"type": "heatmap", "title": f"{tlab}·{_g_friendly(dim_pair[0])}×{_g_friendly(dim_pair[1])}分布",
                      "table": table, "table_label": tlab,
                      "metric_label": _g_friendly(measures[0]),
                      "dim_label": f"{_g_friendly(dim_pair[0])}/{_g_friendly(dim_pair[1])}",
                      "interpretation": _interpret_heatmap(c["rows"]),
                      "insight_type": "shape", "severity": 0.5})
            _budget_take(type_budget, "heatmap")
            return c
    if len(measures) >= 2 and len(dims) == 1 and not times and _budget_ok(type_budget, "radar"):
        c = _g_radar(table, tlab, dims[0], measures[:4])
        if c:
            c.update({"type": "radar", "title": f"{tlab}·各{c['d_lab']}多指标横评",
                      "table": table, "table_label": tlab,
                      "metric_label": "/".join(c["m_labs"]), "dim_label": c["d_lab"],
                      "interpretation": _interpret_radar(c["rows"], c["d_lab"], c["m_labs"]),
                      "insight_type": "shape", "severity": 0.5})
            _budget_take(type_budget, "radar")
            return c
    if len(measures) >= 2 and not dims and not times and _budget_ok(type_budget, "scatter"):
        c = _g_scatter(table, tlab, measures[0], measures[1])
        if c:
            c.update({"type": "scatter", "title": f"{tlab}·{c['columns'][0]} vs {c['columns'][1]}",
                      "table": table, "table_label": tlab,
                      "metric_label": c["columns"][0], "dim_label": c["columns"][1],
                      "interpretation": _interpret_scatter(c["rows"], c["columns"][0], c["columns"][1]),
                      "insight_type": "shape", "severity": 0.5})
            _budget_take(type_budget, "scatter")
            return c
    if len(dims) >= 1 and len(measures) >= 1 and _budget_ok(type_budget, "rank"):
        # 维度选评分最好的（业务维度优先，避免按纯外键分组）
        dim0 = sorted(dims, key=_dim_score)[0]
        c = _g_dim_measure(table, tlab, dim0, measures[0])
        if c:
            ctype = _pick_rank_type(c["rows"], c["vals"], c["d_lab"])
            c.update({"type": ctype, "title": f"{tlab}·各{c['d_lab']}的{c['m_lab']}",
                      "table": table, "table_label": tlab,
                      "metric_label": c["m_lab"], "dim_label": c["d_lab"],
                      "interpretation": _interpret_rank(c["rows"], c["d_lab"], c["m_lab"], ctype),
                      "insight_type": "shape", "severity": 0.5})
            _budget_take(type_budget, "rank")
            return c
    return None


def _build_generic_problem_charts(picked, counts, type_budget: dict | None = None):
    """对重点表跑 insight_scan，把发现的重点问题做成图表（任意库通用）。"""
    from db.tools import get_table_detail
    from agent.insight_scan import scan_table
    charts: list[dict] = []
    used: set[str] = set()
    ranked = sorted(picked, key=lambda t: counts.get(t["table_name"]) or 0, reverse=True)
    for t in ranked[:4]:
        table = t["table_name"]
        if table in used:
            continue
        if (counts.get(table) or 0) < _GENERIC_MIN_ROWS:
            continue
        try:
            fields = (get_table_detail(table) or {}).get("fields") or []
        except Exception:
            continue
        if not fields:
            continue
        try:
            res = scan_table(table, max_measures=2, max_dims=3, fields=fields)
        except Exception:
            continue
        insights = (res or {}).get("insights") or []
        for it in insights[:2]:
            chart = _problem_to_chart(table, t, fields, it)
            if chart and chart["id"] not in {c["id"] for c in charts}:
                # 类型多样性：同形态超限的问题图跳过（换下一条洞察）
                grp = _g_type_group(chart["type"])
                if not _budget_ok(type_budget, grp):
                    continue
                _budget_take(type_budget, grp)
                _attach_lineage(chart, table, _tlabel(t, table))
                charts.append(chart)
                used.add(table)
                break
    return charts


def _build_shape_charts(picked, counts, max_n, used_tables, type_budget: dict | None = None):
    """数据形态驱动的补充图：为尚未出图的重点表各选一张最合适形态的图。"""
    from db.tools import get_table_detail
    from agent.insight_scan import _split_fields
    charts: list[dict] = []
    ranked = sorted(picked, key=lambda t: counts.get(t["table_name"]) or 0, reverse=True)
    for t in ranked:
        if len(charts) >= max_n:
            break
        table = t["table_name"]
        if table in used_tables:
            continue
        if (counts.get(table) or 0) < _GENERIC_MIN_ROWS:
            continue
        try:
            fields = (get_table_detail(table) or {}).get("fields") or []
        except Exception:
            continue
        if not fields:
            continue
        try:
            measures, dims, times = _split_fields(fields)
        except Exception:
            continue
        if not measures:
            continue
        chart = _shape_to_chart(table, t, fields, measures, dims, times, type_budget)
        if chart:
            chart["id"] = f"generic:shape:{table}:{chart['type']}"
            _attach_lineage(chart, table, _tlabel(t, table))
            charts.append(chart)
            used_tables.add(table)
    return charts


def _build_manufacturing_charts(picked, max_n, used_tables):
    """制造业创意图富化（gauge/sunburst/pareto/funnel/stock/stacked），仅字段命中时产出。"""
    from db.tools import get_table_detail
    buckets: dict[str, list[tuple]] = {
        "gauge": [], "funnel": [], "sunburst": [], "pareto": [], "stock": [], "stacked": [],
    }
    for t in picked:
        table = t["table_name"]
        tlab = _tlabel(t, table)
        try:
            fields = (get_table_detail(table) or {}).get("fields") or []
        except Exception:
            continue
        if not fields:
            continue
        entry = (table, tlab, fields)
        if _has_field(fields, "good_qty", "qualified_quantity") and _has_field(fields, "input_qty", "output_qty", "produced_quantity"):
            buckets["gauge"].append(entry)
        if _has_field(fields, "process_id", "process_name"):
            buckets["funnel"].append(entry)
        if _has_field(fields, "severity") and _has_field(fields, "defect_type", "type"):
            buckets["sunburst"].append(entry)
        if _has_field(fields, "reason") and _has_field(fields, "downtime_minutes", "duration"):
            buckets["pareto"].append(entry)
        if _has_field(fields, "available_qty", "stock_qty", "quantity") and _has_field(fields, "safety"):
            buckets["stock"].append(entry)
        if _has_field(fields, "good_qty", "qualified_quantity") and _has_field(fields, "defect_qty", "defect_quantity"):
            buckets["stacked"].append(entry)
    builders = [
        ("gauge", _build_gauge), ("sunburst", _build_sunburst), ("pareto", _build_pareto),
        ("stock", _build_stock_health), ("funnel", _build_funnel), ("stacked", _build_stacked),
    ]
    charts: list[dict] = []
    for key, builder in builders:
        if len(charts) >= max_n:
            break
        for table, tlab, fields in buckets.get(key, []):
            if table in used_tables:
                continue
            try:
                c = builder(table, tlab, fields)
            except Exception:
                c = None
            if c:
                _attach_lineage(c, table, tlab)
                charts.append(c)
                used_tables.add(table)
                break
    return charts


# ── 主入口 ────────────────────────────────────────────

def generate_overview_charts(limit: int = 3, max_charts: int = MAX_CHARTS) -> dict:
    """自动探查当前库，生成「重点问题驱动 + 数据形态驱动」的可解释创意图表。

    返回 {success, charts, error}
    生成顺序（保证类型多样、贴合数据重点，任意数据库动态适配）：
      1) 通用重点问题图：insight_scan 发现离群/失衡/趋势突变 → barh/donut/line/area
      2) 数据形态驱动补充图：趋势→line、双维→heatmap、多指标→radar、双数值→scatter、
         构成→rose/donut、排行→barh —— 绝不全是一种柱状图
      3) 制造业创意图富化：gauge/sunburst/pareto/funnel/stock/stacked（字段命中才产出）
    """
    # TTL 缓存：30 秒内同库复用（总览页每次进入都触发全库扫描约 40 条 SQL）
    _ck = _overview_cache_key()
    if _ck is not None and _OVERVIEW_CACHE["key"] == _ck and time.time() - _OVERVIEW_CACHE["ts"] < _OVERVIEW_TTL:
        return _OVERVIEW_CACHE["value"]

    try:
        from db.tools import get_all_tables, get_table_detail
        from db.executor import get_table_row_counts
        tables = get_all_tables() or []
        counts = get_table_row_counts() or {}
    except Exception as e:
        return {"success": False, "charts": [], "error": str(e)[:160]}

    # 表级 ACL（2026-09-24 修）：本函数取的是**全库表清单**再挑重点表扫，
    # 而 SQL 走 `_safe_execute`，那里只做只读校验、**不做表级权限过滤** ——
    # 于是受限角色（如 viewer）能通过总览页读到无权表的聚合数值。
    # 与 `routers/tables.py:get_attention_points` 的 P0 修法保持一致：
    # 在**表清单层**按当前用户白名单过滤（fail-closed）。
    try:
        from security.context import get_acl as _get_acl
        _acl = _get_acl()
        if _acl is not None and not getattr(_acl, "superuser", False) \
                and _acl.allowed_tables is not None:
            _allow = {t.split(".")[-1].lower() for t in _acl.allowed_tables}
            tables = [t for t in tables
                      if str(t.get("table_name") or "").split(".")[-1].lower() in _allow]
            if not tables:
                return {"success": False, "charts": [],
                        "error": "当前角色没有可访问的数据表，请联系管理员开通权限"}
    except Exception:
        pass

    ranked = sorted(
        tables,
        key=lambda t: counts.get(t.get("table_name")) or t.get("row_count") or 0,
        reverse=True,
    )
    picked = [t for t in ranked
              if not str(t.get("table_name") or "").lower().startswith("test_")
              and (counts.get(t.get("table_name")) or t.get("row_count") or 0) >= 3]
    if not picked:
        return {"success": False, "charts": [], "error": "没有数据量足够的表可供分析"}

    charts: list[dict] = []
    used_tables: set[str] = set()
    # 类型多样性预算：同一形态（趋势/热力/雷达/散点/单维）一批最多 _MAX_SAME_TYPE 张
    type_budget: dict[str, int] = {}

    # 1) 制造业创意图富化（gauge/sunburst/pareto 等，仅在制造业字段命中时产出，最多 3 张）
    mf_limit = min(3, max_charts)
    charts.extend(_build_manufacturing_charts(picked, mf_limit, used_tables))
    used_tables = {c["table"] for c in charts}

    # 2) 通用重点问题图（离群/失衡/趋势突变 → 多样化图表，任意库可用）
    charts.extend(_build_generic_problem_charts(picked, counts, type_budget))
    used_tables = {c["table"] for c in charts}

    # 3) 数据形态驱动补充图（保证类型多样，避免全是一种）
    charts.extend(_build_shape_charts(picked, counts, max_charts - len(charts), used_tables, type_budget))
    used_tables = {c["table"] for c in charts}

    # 4) 若仍有空位，允许制造业同表补充其余创意图（funnel/stock/stacked）
    if len(charts) < max_charts:
        charts.extend(_build_manufacturing_charts(picked, max_charts - len(charts), used_tables))

    # 去重（按 id）并截断
    seen: set[str] = set()
    out: list[dict] = []
    for c in charts:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        # 兜底：任何路径漏掉的 chart 都补一条最小溯源（表名 + 空字段/SQL）
        if "lineage" not in c:
            _attach_lineage(c, c.get("table") or "", c.get("table_label") or "")
        out.append(c)
        if len(out) >= max_charts:
            break

    if not out:
        result = {"success": False, "charts": [], "error": "未生成图表（数据不足以形成有意义的分析）"}
    else:
        result = {"success": True, "charts": out, "error": ""}
    if _ck is not None:
        _OVERVIEW_CACHE["key"] = _ck
        _OVERVIEW_CACHE["ts"] = time.time()
        _OVERVIEW_CACHE["value"] = result
    return result
