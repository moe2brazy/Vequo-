# -*- coding: utf-8 -*-
"""算子级血缘单元测试（P2-4，对标 Aloudata BIG）"""
import sys
sys.path.insert(0, '.')

from agent.lineage import build_operator_lineage, _op_tree, _op_chain

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def chain_of(expr):
    l = build_operator_lineage("SELECT %s FROM mes_process_output" % expr)
    c = l["columns"][0] if l["columns"] else {}
    return c.get("chain", "")


print("═══ A. 算子链正确性 ═══")
check("A1 简单聚合", chain_of("SUM(good_qty) AS t") == "SUM(good_qty)",
      chain_of("SUM(good_qty) AS t"))
check("A2 比率", chain_of("good_qty * 1.0 / input_qty AS r") == "/(*(good_qty, 1.0), input_qty)",
      chain_of("good_qty * 1.0 / input_qty AS r"))
check("A3 复合指标（含 ROUND 精度位）",
      chain_of("ROUND(SUM(good_qty) / COUNT(order_id), 2) AS x")
      == "Round(/(SUM(good_qty), COUNT(order_id)), 2)",
      chain_of("ROUND(SUM(good_qty) / COUNT(order_id), 2) AS x"))
check("A4 COALESCE 首参不丢", chain_of("COALESCE(SUM(good_qty), 0) AS t") == "Coalesce(SUM(good_qty), 0)",
      chain_of("COALESCE(SUM(good_qty), 0) AS t"))
check("A5 CASE 分档含分支", "CASE" in chain_of("CASE WHEN good_qty > 100 THEN '高' ELSE '低' END AS l")
      and "高" in chain_of("CASE WHEN good_qty > 100 THEN '高' ELSE '低' END AS l"),
      chain_of("CASE WHEN good_qty > 100 THEN '高' ELSE '低' END AS l"))
check("A6 嵌套乘除", "MUL" in chain_of("SUM(good_qty * unit_price) AS t").upper() or
      "*(good_qty, unit_price)" in chain_of("SUM(good_qty * unit_price) AS t"),
      chain_of("SUM(good_qty * unit_price) AS t"))

print("═══ B. 结构完备 ═══")
l = build_operator_lineage("SELECT SUM(good_qty) AS total, line_name FROM mes_process_output GROUP BY line_name")
check("B1 每列都有 ops 与 chain",
      all("ops" in c and "chain" in c for c in l["columns"]), str(l["columns"]))
check("B2 ops 是算子树结构", all(isinstance(c["ops"], dict) and "kind" in c["ops"] for c in l["columns"]), "")
check("B3 列级血缘字段保留（兼容）", all("table" in c and "agg" in c for c in l["columns"]), "")
agg_col = [c for c in l["columns"] if c.get("agg")][0] if [c for c in l["columns"] if c.get("agg")] else None
check("B4 聚合列 ops 顶层是 SUM", agg_col and agg_col["ops"].get("op") == "SUM", str(agg_col["ops"]) if agg_col else "")

print("═══ C. 边界与降级 ═══")
check("C1 空 SQL 返回空结构", build_operator_lineage("")["columns"] == [], "")
check("C2 非法 SQL 不崩", build_operator_lineage("不是 SQL ###")["columns"] == [], "")
l = build_operator_lineage("SELECT * FROM mes_process_output")
check("C3 星号投影不崩（无 ops）", l["columns"] and "ops" not in l["columns"][0], "")
check("C4 与 build_lineage 结构兼容", "tables" in build_operator_lineage("SELECT 1") and
      "columns" in build_operator_lineage("SELECT 1"), "")

print(f"\n{'='*46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'='*46}")
sys.exit(1 if FAIL else 0)
