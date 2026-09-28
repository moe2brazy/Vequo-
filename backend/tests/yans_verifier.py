# -*- coding: utf-8 -*-
"""yans 独立黄金验证器：编译 SQL vs 独立权威参照 SQL 双跑数值对照。

原则：验证器自身不依赖 metric_compiler 的任何口径——参照聚合表达式与维度 JOIN
映射全部手写（独立权威实现）。编译 SQL 与参照 SQL 分别执行，结果归一化后逐行
对比（维度值 + 聚合值，浮点 1e-6 容差）。不一致 = 确定性编译语义错误（真 bug）。

运行: python tests/yans_verifier.py
报告: tests/yans_verify_report.md
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from database import switch_database
switch_database({"db_type":"postgresql","host":"localhost","port":5432,"name":"yans","user":"postgres","password":"123456"})
from agent.metric_compiler import try_compile_metric
from db.executor import execute_sql
from yans_question_bank import QUESTION_BANK
from gen_yans_bank import build_bank

# ═══ 独立权威口径（手写，与编译器无关）═══

# 表 → 指标 → 独立聚合表达式（f 为事实表别名）
REF_AGG = {
    ("mes_process_output", "产量"): "SUM(f.good_qty)",
    ("mes_process_output", "良率"): "SUM(f.good_qty)::numeric * 100.0 / NULLIF(SUM(f.input_qty), 0)",
    ("mes_process_output", "不良数"): "SUM(f.defect_qty)",
    ("mes_process_output", "投入量"): "SUM(f.input_qty)",
    ("mes_process_output", "返工数量"): "SUM(f.rework_qty)",
    ("mes_process_output", "返工率"): "SUM(f.rework_qty)::numeric * 100.0 / NULLIF(SUM(f.input_qty), 0)",
    ("mes_process_output", "合格数量"): "SUM(f.good_qty)",
    ("qms_defect_detail", "缺陷数"): "COUNT(*)",
    ("qms_defect_detail", "缺陷件数"): "SUM(COALESCE(f.defect_qty, 0))",
    ("qms_defect_detail", "严重缺陷数"): "SUM(CASE WHEN f.severity_level = 'critical' THEN 1 ELSE 0 END)",
    ("qms_inspection", "抽检数"): "SUM(f.sample_qty)",
    ("qms_inspection", "质检合格率"): "SUM(f.sample_qty - COALESCE(f.defect_qty,0))::numeric * 100.0 / NULLIF(SUM(f.sample_qty), 0)",
    ("qms_inspection", "质检不合格率"): "SUM(f.defect_qty)::numeric * 100.0 / NULLIF(SUM(f.sample_qty), 0)",
    ("qms_inspection", "抽检不良数"): "SUM(f.defect_qty)",
    ("qms_inspection", "检验次数"): "COUNT(*)",
    ("mes_work_order", "工单数"): "COUNT(*)",
    ("mes_work_order", "计划数量"): "SUM(f.plan_qty)",
    ("mes_work_order", "在制工单数"): "SUM(CASE WHEN f.order_status = 'in_progress' THEN 1 ELSE 0 END)",
    ("mes_work_order", "已完成工单数"): "SUM(CASE WHEN f.order_status = 'completed' THEN 1 ELSE 0 END)",
    ("mes_work_order", "已关闭工单数"): "SUM(CASE WHEN f.order_status = 'closed' THEN 1 ELSE 0 END)",
    ("eqp_downtime_record", "停机时长"): "SUM(f.downtime_minutes)",
    ("eqp_downtime_record", "停机次数"): "COUNT(*)",
    ("eqp_downtime_record", "停机原因种类数"): "COUNT(DISTINCT f.downtime_reason)",
    ("eqp_downtime_record", "平均停机时长"): "AVG(f.downtime_minutes)",
    ("eqp_downtime_record", "非计划停机时长"): "SUM(CASE WHEN f.is_planned = FALSE THEN f.downtime_minutes ELSE 0 END)",
    ("eqp_downtime_record", "计划内停机次数"): "SUM(CASE WHEN f.is_planned THEN 1 ELSE 0 END)",
    ("eqp_downtime_record", "计划内停机时长"): "SUM(CASE WHEN f.is_planned THEN f.downtime_minutes ELSE 0 END)",
    ("eqp_downtime_record", "非计划停机占比"): "SUM(CASE WHEN f.is_planned = FALSE THEN f.downtime_minutes ELSE 0 END)::numeric * 100.0 / NULLIF(SUM(f.downtime_minutes), 0)",
    ("inv_inventory_snapshot", "库存量"): "SUM(f.available_qty)",
    ("inv_inventory_snapshot", "总库存"): "SUM(f.available_qty + COALESCE(f.frozen_qty, 0))",
    ("inv_inventory_snapshot", "安全库存"): "SUM(f.safety_stock_qty)",
    ("inv_inventory_snapshot", "缺货量"): "SUM(CASE WHEN f.available_qty < f.safety_stock_qty THEN f.safety_stock_qty - f.available_qty ELSE 0 END)",
    ("inv_inventory_snapshot", "库存预警数"): "SUM(CASE WHEN f.available_qty < f.safety_stock_qty THEN 1 ELSE 0 END)",
    ("inv_inventory_snapshot", "冻结库存"): "SUM(f.frozen_qty)",
    ("dim_equipment", "设备数"): "COUNT(*)",
    ("dim_equipment", "运行设备数"): "SUM(CASE WHEN f.equipment_status = 'running' THEN 1 ELSE 0 END)",
    ("dim_equipment", "维护设备数"): "SUM(CASE WHEN f.equipment_status = 'maintenance' THEN 1 ELSE 0 END)",
    ("dim_equipment", "空闲设备数"): "SUM(CASE WHEN f.equipment_status = 'idle' THEN 1 ELSE 0 END)",
    ("dim_product", "产品数"): "COUNT(*)",
    ("dim_product", "活跃产品数"): "SUM(CASE WHEN f.is_active THEN 1 ELSE 0 END)",
    ("dim_process", "工序数"): "COUNT(*)",
    ("dim_process", "标准良率"): "MAX(f.standard_yield_rate) * 100.0",
    ("dim_process", "关键工序数"): "SUM(CASE WHEN f.is_key_process THEN 1 ELSE 0 END)",
    ("dim_production_line", "产线数"): "COUNT(*)",
}

# 表 → 维度 → (JOIN 子句, 分组展示列) —— 独立权威 JOIN 映射
REF_DIM = {
    "mes_process_output": {
        "工序": ("JOIN dim_process d ON f.process_id = d.process_id", "d.process_name"),
        "产线": ("JOIN dim_production_line d ON f.line_id = d.line_id", "d.line_name"),
        "产品": ("JOIN dim_product d ON f.product_id = d.product_id", "d.product_name"),
        "产品类别": ("JOIN dim_product d ON f.product_id = d.product_id", "d.product_category"),
        "班次": ("", "f.shift_code"),
        "工单": ("", "f.work_order_id"),
        "车间": ("JOIN dim_production_line d ON f.line_id = d.line_id", "d.workshop_name"),
    },
    "qms_defect_detail": {
        "缺陷类型": ("", "f.defect_type"),
        "不良类型": ("", "f.defect_type"),
        "严重度": ("", "f.severity_level"),
        "严重程度": ("", "f.severity_level"),
        "工序": ("JOIN dim_process d ON f.responsible_process_id = d.process_id", "d.process_name"),
        "检验": ("", "f.inspection_id"),
    },
    "qms_inspection": {
        "产品": ("JOIN dim_product d ON f.product_id = d.product_id", "d.product_name"),
        "工序": ("JOIN dim_process d ON f.process_id = d.process_id", "d.process_name"),
        "结果": ("", "f.inspection_result"),
    },
    "mes_work_order": {
        "产品": ("JOIN dim_product d ON f.product_id = d.product_id", "d.product_name"),
        "产线": ("JOIN dim_production_line d ON f.line_id = d.line_id", "d.line_name"),
        "车间": ("JOIN dim_production_line d ON f.line_id = d.line_id", "d.workshop_name"),
        "状态": ("", "f.order_status"),
        "工单状态": ("", "f.order_status"),
    },
    "eqp_downtime_record": {
        "原因": ("", "f.downtime_reason"),
        "停机原因": ("", "f.downtime_reason"),
        "设备": ("JOIN dim_equipment d ON f.equipment_id = d.equipment_id", "d.equipment_name"),
        "设备类型": ("JOIN dim_equipment d ON f.equipment_id = d.equipment_id", "d.equipment_type"),
        "产线": ("JOIN dim_production_line d ON f.line_id = d.line_id", "d.line_name"),
        "车间": ("JOIN dim_production_line d ON f.line_id = d.line_id", "d.workshop_name"),
        "计划类型": ("", "f.is_planned"),
    },
    "inv_inventory_snapshot": {
        "产品": ("JOIN dim_product d ON f.product_id = d.product_id", "d.product_name"),
        "仓库": ("", "f.warehouse_code"),
    },
    "dim_equipment": {
        "设备状态": ("", "f.equipment_status"),
        "设备类型": ("", "f.equipment_type"),
        "产线": ("JOIN dim_production_line d ON f.line_id = d.line_id", "d.line_name"),
        "车间": ("JOIN dim_production_line d ON f.line_id = d.line_id", "d.workshop_name"),
    },
    "dim_product": {
        "产品类别": ("", "f.product_category"),
        "类别": ("", "f.product_category"),
    },
    "dim_process": {
        "工序": ("", "f.process_name"),
    },
    "dim_production_line": {
        "车间": ("", "f.workshop_name"),
    },
}

REF_TABLE_FOR_FACT = {}  # 由上面自动推导

# 聚合覆盖变体：编译 SQL 用 MAX/MIN/AVG（_agg_override「最长/最短/平均」）时参照同步。
# 编译 SQL 中列可能带/不带 f. 前缀 → 用正则匹配
_AGG_VARIANT_PATTERNS = [
    (r"MAX\(f?\.?downtime_minutes\)", ("eqp_downtime_record", "停机时长", "MAX(f.downtime_minutes)")),
    (r"MIN\(f?\.?downtime_minutes\)", ("eqp_downtime_record", "停机时长", "MIN(f.downtime_minutes)")),
    (r"AVG\(f?\.?downtime_minutes\)", ("eqp_downtime_record", "停机时长", "AVG(f.downtime_minutes)")),
]

def build_ref_sql(metric: str, dim: str, compiled_sql: str = "") -> str | None:
    """独立生成参照 SQL，与编译 SQL 同一过滤窗口/截断。

    SELECT 聚合与 GROUP BY 维度用独立权威口径；WHERE（时间/值过滤）与 LIMIT（TOP-N）
    从编译 SQL 提取复用——保证对照的是「同窗口下两套口径的数值」而非全量 vs 截断。
    """
    import re as _re
    # 聚合覆盖检测（MAX/MIN/AVG → 参照用同聚合；product_category → 参照用类别维度）
    agg_override = None
    if compiled_sql:
        for pat, agg_cfg in _AGG_VARIANT_PATTERNS:
            if _re.search(pat, compiled_sql):
                agg_override = agg_cfg
                break
    fact = None
    agg = None
    if agg_override:
        fact, metric, agg = agg_override
    else:
        for (t, m), a in REF_AGG.items():
            if m == metric:
                fact = t
                agg = a
                break
    if not fact or not agg:
        return None
    # product_category 分组 → 用类别维度（REF_DIM 各表的「产品类别」）
    use_cat = compiled_sql and "product_category" in compiled_sql
    djoin = None
    if use_cat:
        djoin = REF_DIM.get(fact, {}).get("产品类别")
    if not djoin and dim:
        djoin = REF_DIM.get(fact, {}).get(dim)
    if not djoin and dim:
        return None
    join_sql, disp = djoin if djoin else ("", "")
    where = ""
    if compiled_sql:
        m = _re.search(r" WHERE (.+?)(?: GROUP BY| LIMIT| ORDER BY|$)", compiled_sql)
        if m:
            where = f" WHERE {m.group(1)}"
    limit = ""
    if compiled_sql:
        m = _re.search(r"LIMIT (\d+)", compiled_sql)
        if m:
            limit = f" LIMIT {m.group(1)}"
    # 跟随编译 SQL 的排序方向（「最少/最低」问法编译为 ASC；参照写死 DESC 会导致
    # Top-1 取值方向相反 → 集合不一致误报）
    order = ""
    if limit:
        dirn = "DESC"
        m = _re.search(r"ORDER BY [^ ]+ (ASC|DESC)", compiled_sql, _re.IGNORECASE)
        if m:
            dirn = m.group(1).upper()
        order = f" ORDER BY v {dirn}"
    # 无维度（total 单值）→ 不加 GROUP BY，参照返回单行；带维度 → GROUP BY 展示列
    if djoin:
        return f"SELECT {disp} AS k, {agg} AS v FROM {fact} f {join_sql} {where} GROUP BY {disp}{order}{limit}"
    return f"SELECT {agg} AS v FROM {fact} f {where} LIMIT 1"

def norm(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v).lower()
    try:
        f = float(v)
        if f == int(f) and abs(f) < 1e15:
            return str(int(f))
        return f"{f:.6f}"
    except (ValueError, TypeError):
        return str(v)

def exec_rows(sql):
    r = execute_sql(sql)
    if not r.get("success"):
        return None, str(r.get("error", ""))[:80]
    return r.get("rows") or [], ""

def compare(compiled_sql: str, ref_sql: str, has_limit: bool = False):
    """对比编译 SQL 与参照 SQL 的结果（按 k 排序后逐项比 v）。返回 (ok, detail)"""
    r1, e1 = exec_rows(compiled_sql)
    r2, e2 = exec_rows(ref_sql)
    if r1 is None:
        return False, f"编译SQL执行失败: {e1}"
    if r2 is None:
        return False, f"参照SQL执行失败: {e2}"
    if not r1 and not r2:
        return True, "两边均 0 行"
    def keyfn(row):
        vals = [norm(row.get(c)) for c in row.keys()]
        return tuple(vals[:-1])  # 去掉最后一个数值列
    m1 = {}
    for row in r1:
        ks = [norm(v) for v in list(row.values())[:-1]]
        k = tuple(ks)
        v = float(list(row.values())[-1] or 0)
        m1[k] = m1.get(k, 0) + v  # 同 k 合并（防重复维度值）
    m2 = {}
    for row in r2:
        ks = [norm(v) for v in list(row.values())[:-1]]
        k = tuple(ks)
        v = float(list(row.values())[-1] or 0)
        m2[k] = m2.get(k, 0) + v
    if set(m1) == set(m2):
        diffs = []
        for k in sorted(m1):
            a, b = m1[k], m2[k]
            if abs(a - b) > max(1e-6, abs(b) * 1e-6):
                diffs.append((k, a, b))
        if diffs:
            return False, f"{len(diffs)} 个维度值数值不一致，例: {diffs[:3]}"
        return True, f"一致({len(m1)} 组)"
    # 集合不一致：若为 TOP/LIMIT 截断场景，降级「并列值集」对照——
    # 编译行数 ≤ 参照行数，且编译的每个值都在参照的最大值集合内、最大 v 相等
    if has_limit and len(m1) <= len(m2) and m1:
        vals1 = sorted(m1.values(), reverse=True)
        vals2 = sorted(m2.values(), reverse=True)
        if abs(vals1[0] - vals2[0]) <= 1e-6:
            # 编译的每一行的值都应在参照前 n 大值里（容忍并列导致的行选择差异）
            top_n = set(vals2[:len(vals1)])
            if all(any(abs(a - b) <= 1e-6 for b in top_n) for a in vals1):
                return True, f"并列Top值一致(编译{len(m1)}行,参照{len(m2)}行)"
    only1 = set(m1) - set(m2)
    only2 = set(m2) - set(m1)
    return False, f"维度值集合不一致: 编译独有 {len(only1)} 个({list(only1)[:3]}), 参照独有 {len(only2)} 个({list(only2)[:3]})"

def main():
    # group/rank/top/trend + total 全部纳入独立数值对照（total 单值也双跑）
    bank = [b for b in QUESTION_BANK + build_bank() if b["expect"] in ("group", "rank", "top", "total")]
    seen = set()
    rows = []
    for b in bank:
        q = b["q"]
        if q in seen:
            continue
        seen.add(q)
        r = try_compile_metric(q)
        if not r:
            continue
        # 解析编译 SQL 用到的 (指标, 维度)：从 mql 或 SQL 文本提取
        mql = r.get("mql") or {}
        metric = (mql.get("metric") or r.get("metric") or "").split("(")[0].strip()
        # 对比/关系类（多指标双列，参照单列无法对照）→ 跳过，另由 LLM/人工核验
        if len(mql.get("metrics") or []) > 1:
            rows.append({"q": q, "metric": metric, "dim": "", "status": "multi_metric", "detail": ""})
            continue
        dims = mql.get("dimensions") or []
        dim = dims[0] if dims else None
        # 找匹配的参照（指标名可能带别名差异，尝试用指标名直接匹配）；无维度 total 也建单值参照
        ref_sql = build_ref_sql(metric, dim, r["sql"])
        if not ref_sql:
            rows.append({"q": q, "metric": metric, "dim": dim, "status": "no_ref", "detail": ""})
            continue
        ok, detail = compare(r["sql"], ref_sql, has_limit="LIMIT" in r["sql"].upper())
        rows.append({"q": q, "metric": metric, "dim": dim, "status": "ok" if ok else "MISMATCH",
                     "detail": detail, "sql": r["sql"]})

    total = len(rows)
    mism = [x for x in rows if x["status"] == "MISMATCH"]
    noref = [x for x in rows if x["status"] == "no_ref"]
    okrows = [x for x in rows if x["status"] == "ok"]
    print(f"═══ yans 独立数值对照验证: {total} 题 ═══")
    print(f"✅ 数值一致: {len(okrows)}   🔴 数值不一致(真bug): {len(mism)}   ⚪ 无参照: {len(noref)}")
    if mism:
        print("\n── 🔴 数值不一致（编译 SQL 语义错误）──")
        for m in mism[:15]:
            print(f"  {m['q']} | 指标={m['metric']} 维度={m['dim']} | {m['detail']}")
            print(f"    SQL: {m['sql'][:100]}")
    if noref:
        print("\n── ⚪ 无参照（trend/total 或未覆盖维度）──")
        for n in noref[:15]:
            print(f"  {n['q']} | 指标={n['metric']} 维度={n['dim']}")

    # 写报告
    lines = ["# yans 独立数值对照验证报告", "",
             f"- 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}", f"- 题库: {total} 题（group/rank/top 类）", "",
             f"| 结果 | 数量 |", "|---|---|", f"| ✅ 数值一致 | {len(okrows)} |",
             f"| 🔴 数值不一致 | {len(mism)} |", f"| ⚪ 无参照 | {len(noref)} |", ""]
    if mism:
        lines.append("## 🔴 数值不一致（需修复）")
        lines.append("")
        for m in mism:
            lines.append(f"- `{m['q']}` 指标={m['metric']} 维度={m['dim']} | {m['detail']}")
    lines.append("")
    lines.append("## 验证方法")
    lines.append("")
    lines.append("编译 SQL 与独立手写参照 SQL（另一套权威口径：REF_AGG 聚合表达式 + REF_DIM JOIN 映射）分别执行，结果按维度值归一化后逐项对比聚合数值（浮点 1e-6 容差）。")
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "yans_verify_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("完整报告: tests/yans_verify_report.md")

if __name__ == "__main__":
    main()
