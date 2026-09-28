# -*- coding: utf-8 -*-
"""结果溯源（血缘）单元测试：确定性解析的各类 SQL 场景（含边界与回归防护）"""
import sys
sys.path.insert(0, '.')

from agent.lineage import build_lineage

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

def cols_of(sql, dialect=""):
    return build_lineage(sql, dialect=dialect)["columns"]

# ── A. 单表场景 ──
print("═══ A. 单表场景 ═══")
c = cols_of("SELECT process_id, SUM(good_qty) AS total FROM mes_process_output GROUP BY process_id")
check("A1 裸维度列归属单表", len(c) == 2 and c[0]["table"] == "mes_process_output" and c[0]["field"] == "process_id")
check("A2 聚合列归属+agg", c[1]["table"] == "mes_process_output" and c[1]["field"] == "good_qty" and c[1]["agg"] == "SUM")
check("A3 表中文名", build_lineage("SELECT * FROM mes_process_output")["tables"][0]["label"] == "工序产量表")
check("A4 字段描述", c[1]["field_desc"] != "")

# ── B. 别名/前缀 ──
print("═══ B. 别名前缀 ═══")
c = cols_of("SELECT mp.process_id, SUM(mp.good_qty) AS total FROM mes_process_output mp GROUP BY mp.process_id")
check("B1 前缀解析到真实表", c[0]["table"] == "mes_process_output" and c[0]["field"] == "process_id")
check("B2 别名表名", [t["alias"] for t in build_lineage("SELECT mp.process_id FROM mes_process_output mp")["tables"]] == ["mp"])

# ── C. 多表 JOIN ──
print("═══ C. 多表 JOIN ═══")
c = cols_of("SELECT SUM(good_qty) AS total FROM mes_process_output mp JOIN dim_product p ON mp.product_id = p.product_id")
check("C1 裸聚合列唯一命中", c[0]["table"] == "mes_process_output" and c[0]["field"] == "good_qty")
c = cols_of("SELECT product_id FROM mes_process_output mp JOIN dim_product p ON mp.product_id = p.product_id LIMIT 10")
check("C2 歧义裸列留空不瞎猜", c[0]["table"] == "" and c[0]["field"] == "product_id")
c = cols_of("SELECT p.product_id FROM mes_process_output mp JOIN dim_product p ON mp.product_id = p.product_id LIMIT 10")
check("C3 带前缀歧义列归属正确", c[0]["table"] == "dim_product")

# ── D. 聚合边界 ──
print("═══ D. 聚合边界 ═══")
c = cols_of("SELECT COUNT(*) AS cnt FROM mes_process_output")
check("D1 COUNT(*) 全表计数", c[0]["field"] == "*" and c[0]["agg"] == "COUNT")
c = cols_of("SELECT COUNT(1) AS cnt FROM mes_process_output")
check("D2 COUNT(1) 全表计数", c[0]["field"] == "*" and c[0]["agg"] == "COUNT")
c = cols_of("SELECT SUM(CASE WHEN mp.shift_code = 'D' THEN mp.good_qty ELSE 0 END) AS d_qty FROM mes_process_output mp")
check("D3 CASE 聚合取结果列", c[0]["field"] == "good_qty" and c[0]["agg"] == "SUM")
c = cols_of("SELECT process_id, SUM(good_qty) AS g, AVG(good_qty) AS avg_q FROM mes_process_output GROUP BY process_id")
check("D4 多指标独立聚合", len(c) == 3 and c[1]["agg"] == "SUM" and c[2]["agg"] == "AVG")

# ── E. CTE / 派生表 ──
print("═══ E. CTE / 派生表 ═══")
out = build_lineage("WITH t AS (SELECT good_qty FROM mes_process_output) SELECT COUNT(*) AS cnt FROM t")
check("E1 无 CTE 幽灵表", [t["name"] for t in out["tables"]] == ["mes_process_output"])
check("E2 CTE 内聚合归属", out["columns"][0]["table"] == "mes_process_output" and out["columns"][0]["field"] == "*")
c = cols_of("SELECT d, total FROM (SELECT stat_date AS d, SUM(good_qty) AS total FROM mes_process_output GROUP BY stat_date) s ORDER BY d DESC LIMIT 7")
check("E3 子查询别名回溯 d→stat_date", c[0]["field"] == "stat_date" and c[0]["table"] == "mes_process_output")
check("E4 子查询别名回溯 total→SUM(good_qty)", c[1]["field"] == "good_qty" and c[1]["agg"] == "SUM")

# ── F. 其他 ──
print("═══ F. 其他 ═══")
c = cols_of("SELECT 1 AS one")
check("F1 常量列无归属", c[0]["table"] == "" and c[0]["field"] == "")
c = cols_of("SELECT * FROM mes_process_output")
check("F2 SELECT * 归属单表", c[0]["table"] == "mes_process_output" and c[0]["field"] == "*")
c = cols_of("SELECT SUM(good_qty) AS t FROM factory.mes_process_output")
check("F3 schema 表名", c[0]["table"] == "factory.mes_process_output" and c[0]["field_desc"] != "")
out = build_lineage("")
check("F4 空 SQL 返回空结构", out == {"tables": [], "columns": []})
out = build_lineage("SELECT * FROM `mes_process_output` WHERE `shift_code` = 'D'", dialect="mysql")
check("F5 MySQL 反引号方言", [t["name"] for t in out["tables"]] == ["mes_process_output"])
out = build_lineage("SELECT `process_id`, SUM(`good_qty`) AS total FROM `mes_process_output` GROUP BY `process_id`")
check("F6 反引号自动探测方言", len(out["columns"]) == 2 and out["columns"][1]["field"] == "good_qty")
out = build_lineage("THIS IS NOT SQL AT ALL !!!")
check("F7 非法 SQL 返回空结构", out == {"tables": [], "columns": []})

print(f"\n═══ 结果: PASS={PASS} FAIL={FAIL} ═══")
sys.exit(1 if FAIL else 0)
