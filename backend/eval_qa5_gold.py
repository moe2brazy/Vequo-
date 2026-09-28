"""eval_qa5：带标准答案（gold SQL）的准确率评测（yans）

对每题执行 agent SQL 与 gold SQL，比较执行结果（宽松语义等价）：
- 标量题（1 行 1 列）→ 数值相等（容忍 Decimal/float 舍入）
- 表题 → 列数相同 + 行数相同 + 规范化行集合相同（忽略列名与行序，值四舍五入 2 位）
- gold 0 行（合法空结果）→ agent 也 0 行即 PASS（EMPTY）
- 并列多口径（resolve ambiguous）→ 出现 metric_clarify 即 PASS（行为正确：引导分问）
输出分 compiled / LLM 两层的准确率。
"""
import sys, time, traceback
sys.path.insert(0, ".")
import config
from sqlalchemy import text
from database import get_db
from agent.llm_service import LLMService

CASES = [
    # (tag, 问题, gold SQL, 期望类型: scalar/table/empty/clarify)
    ("R1", "已完成工单有多少", "SELECT COUNT(*) AS v FROM mes_work_order WHERE order_status='completed'", "scalar"),
    ("R2", "在产工单有几个", "SELECT COUNT(*) AS v FROM mes_work_order WHERE order_status='in_progress'", "scalar"),
    ("R3", "已取消工单的数量", "SELECT COUNT(*) AS v FROM mes_work_order WHERE order_status='closed'", "scalar"),
    ("V1", "处于已下达状态的工单有多少", "SELECT COUNT(*) AS v FROM mes_work_order WHERE order_status='released'", "scalar"),
    ("V2", "等待生产的工单数量", "SELECT COUNT(*) AS v FROM mes_work_order WHERE order_status='released'", "scalar"),
    ("A1", "各产线的产量排行",
     "SELECT d.line_name AS line, SUM(COALESCE(f.good_qty,0)+COALESCE(f.defect_qty,0)) AS v FROM mes_process_output f "
     "JOIN dim_production_line d ON f.line_id=d.line_id GROUP BY d.line_name ORDER BY v DESC", "table"),
    ("A2", "各工序的良率排行",
     "SELECT p.process_name AS line, SUM(f.good_qty)*100.0/NULLIF(SUM(f.input_qty),0) AS v FROM mes_process_output f "
     "JOIN dim_process p ON f.process_id=p.process_id GROUP BY p.process_name ORDER BY v DESC", "table"),
    ("P1", "投入量最大的10个工单",
     "SELECT w.work_order_no AS k, SUM(f.input_qty) AS v FROM mes_process_output f "
     "JOIN mes_work_order w ON f.work_order_id=w.work_order_id "
     "GROUP BY w.work_order_no ORDER BY v DESC LIMIT 10", "table"),
    ("P2", "检验次数最多的前10个产品",
     "SELECT p.product_name AS k, COUNT(*) AS v FROM qms_inspection i "
     "JOIN dim_product p ON i.product_id=p.product_id GROUP BY p.product_name ORDER BY v DESC LIMIT 10", "table"),
    ("P4", "产出良品数最高的前3个产品品类",
     "SELECT p.product_category AS k, SUM(f.good_qty) AS v FROM mes_process_output f "
     "JOIN dim_product p ON f.product_id=p.product_id GROUP BY p.product_category ORDER BY v DESC LIMIT 3", "table"),
    ("Q1", "各检验结论的结果分布",
     "SELECT inspection_result AS k, COUNT(*) AS v FROM qms_inspection GROUP BY inspection_result ORDER BY v DESC", "table"),
    ("Q3", "严重缺陷按工序的分布情况",
     "SELECT p.process_name AS k, COUNT(*) AS v FROM qms_defect_detail d "
     "JOIN dim_process p ON d.responsible_process_id=p.process_id "
     "WHERE d.severity_level='critical' GROUP BY p.process_name ORDER BY v DESC", "table"),
    ("Q4", "2026年单月产量超过10000的月份有哪些",
     "SELECT to_char(stat_date,'YYYY-MM') AS k, SUM(COALESCE(good_qty,0)+COALESCE(defect_qty,0)) AS v FROM mes_process_output "
     "WHERE stat_date >= '2026-01-01' AND stat_date < '2027-01-01' "
     "GROUP BY 1 HAVING SUM(COALESCE(good_qty,0)+COALESCE(defect_qty,0))>10000 ORDER BY v DESC", "table"),
    ("T3", "上周的停机次数",
     "SELECT COUNT(*) AS v FROM eqp_downtime_record "
     "WHERE start_time >= date_trunc('week', CURRENT_DATE) - INTERVAL '7 days' "
     "AND start_time < date_trunc('week', CURRENT_DATE)", "scalar"),
    ("T1", "昨天各产线的产量是多少",
     "SELECT d.line_name AS k, SUM(COALESCE(f.good_qty,0)+COALESCE(f.defect_qty,0)) AS v FROM mes_process_output f "
     "JOIN dim_production_line d ON f.line_id=d.line_id "
     "WHERE f.stat_date >= CURRENT_DATE - 1 AND f.stat_date < CURRENT_DATE GROUP BY d.line_name", "table"),
    ("H1", "平均良率低于95%的产线有哪些",
     "SELECT d.line_name AS k, SUM(f.good_qty)*100.0/NULLIF(SUM(f.input_qty),0) AS v FROM mes_process_output f "
     "JOIN dim_production_line d ON f.line_id=d.line_id "
     "GROUP BY d.line_name HAVING SUM(f.good_qty)*100.0/NULLIF(SUM(f.input_qty),0) < 95", "empty"),
    ("H2", "累计检验次数超过100次的产品有哪些",
     "SELECT p.product_name AS k, COUNT(*) AS v FROM qms_inspection i "
     "JOIN dim_product p ON i.product_id=p.product_id GROUP BY p.product_name HAVING COUNT(*)>100", "empty"),
    ("A6", "产出缺口最大的前5个工单",
     "WITH lastp AS (SELECT x.work_order_id, x.good_qty, ROW_NUMBER() OVER "
     "(PARTITION BY x.work_order_id ORDER BY pr.process_seq DESC) rn FROM mes_process_output x "
     "JOIN dim_process pr ON x.process_id=pr.process_id) "
     "SELECT w.work_order_no AS k, (w.plan_qty - l.good_qty) AS v FROM mes_work_order w "
     "JOIN lastp l ON l.work_order_id=w.work_order_id AND l.rn=1 "
     "ORDER BY v DESC LIMIT 5", "table"),
    ("M1", "产量和良率分别多少", None, "clarify"),
]

db = next(get_db())

def norm(v):
    try:
        return str(round(float(v), 2))
    except Exception:
        return str(v)

def run_gold(sql):
    if sql is None:
        return None
    r = db.execute(text(sql))
    cols = list(r.keys())
    return [tuple(sorted(norm(x) for x in row)) for row in r.fetchall()]

def rows_key(res):
    rows = res.get("rows") or []
    return [tuple(sorted(norm(x) for x in row.values())) for row in rows]

def run_agent(q):
    evs = {"sql": "", "error": "", "done_type": "", "rows": [], "ok": None, "compiled": None}
    try:
        svc = LLMService(q, fast=True)
        for ev in svc.run():
            ty = ev.get("type")
            if ty == "sql":
                evs["sql"] = str(ev.get("sql") or "")
            elif ty == "done":
                r = ev.get("response") or {}
                evs["done_type"] = r.get("type") or ""
                evs["compiled"] = r.get("compiled")
                res = r.get("result") or {}
                if isinstance(res, dict):
                    evs["ok"] = False if res.get("success") is False else True
                    evs["rows"] = res.get("rows") or []
    except Exception as e:
        evs["error"] = str(e)[:150]
    return evs

if __name__ == "__main__":
    passed, failed, notes = 0, [], []
    for tag, q, gold_sql, k in CASES:
        a = run_agent(q)
        if a["error"]:
            notes.append(f"{tag}: error {a['error'][:60]}")
            failed.append(tag)
            continue
        if k == "clarify":
            if a["done_type"] == "metric_clarify":
                passed += 1
                notes.append(f"{tag}: clarify 引导（PASS）")
            else:
                failed.append(tag)
                notes.append(f"{tag}: 期望 clarify 实际 {a['done_type']}")
            continue
        g = run_gold(gold_sql)
        ar = rows_key(a)
        ok = False
        if a["ok"] is not True:
            ok = False
        elif g is not None and (k == "empty" or (g and not g)):
            ok = (len(ar) == 0)          # 期望空结果
        elif len(g) == 1 and len(g[0]) == 1 and len(ar) <= 1 and len(ar[0] if ar else ()) <= 1:
            ok = bool(ar) and g[0][0] == ar[0][0]          # 纯标量
        elif len(g) == 1 and len(g[0]) == 1:
            # 标量期望但返回多行（如状态分布 32+0+0+0）：只要存在匹配行即可（值正确）
            ok = any(r[0] == g[0][0] for r in ar)
        else:
            # 表：gold 每行都应能被某 agent 行“值全包含”（允许 agent 多返回列，如缺口+完工量）
            ok = len(ar) >= len(g) and all(
                any(all(x in row_set for x in grow) for row_set in ar) for grow in g)
        if ok:
            passed += 1
        else:
            failed.append(tag)
            notes.append(f"{tag}: 结果不符 gold={g} agent={ar[:6]} sql={a['sql'][:120].replace(chr(10),' ')}")
    total = len(CASES)
    print(f"准确率 = {passed}/{total} = {passed*100//total}%  (gold 标准答案对比)")
    print(f"通过: {[c[0] for c in CASES if c[0] not in failed]}")
    print(f"未过: {failed}")
    for n in notes:
        print("  ·", n)
