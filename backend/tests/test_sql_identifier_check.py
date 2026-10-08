# -*- coding: utf-8 -*-
"""验证 _unknown_identifiers 修复：别名不再误杀，真幻觉仍能抓到"""
import os, sys
sys.path.insert(0, r"D:\vequo\vequo-viqueo\backend")
os.chdir(r"D:\vequo\vequo-viqueo\backend")
from database import switch_database
switch_database({"db_type": "postgresql", "host": "localhost", "port": 5432,
                 "name": "yans", "user": "postgres", "password": "123456"})
import agent.llm_service as LS

CASES = [
    # (说明, SQL, 期望 unknown 是否为空)
    ("输出别名 + ORDER BY 引用别名",
     "SELECT d.product_name AS \"产品\", SUM(f.defect_qty) AS total_defect "
     "FROM mes_process_output f JOIN dim_product d ON f.product_id = d.product_id "
     "GROUP BY d.product_name ORDER BY total_defect DESC LIMIT 1", True),

    ("窗口占比 + 输出别名",
     "SELECT l.workshop_name, SUM(o.good_qty + o.defect_qty) AS total_output, "
     "ROUND(CAST(SUM(o.good_qty + o.defect_qty) AS DECIMAL) "
     "/ NULLIF(SUM(SUM(o.good_qty + o.defect_qty)) OVER (), 0) * 100, 2) AS ratio_pct "
     "FROM mes_process_output AS o JOIN dim_production_line AS l ON o.line_id = l.line_id "
     "GROUP BY l.workshop_name ORDER BY total_output DESC", True),

    ("表别名 f / d",
     "SELECT d.process_name, SUM(f.good_qty) FROM mes_process_output f "
     "JOIN dim_process d ON f.process_id = d.process_id GROUP BY d.process_name", True),

    ("CTE + 别名",
     "WITH base AS (SELECT product_id, SUM(defect_qty) AS dd FROM mes_process_output "
     "GROUP BY product_id) SELECT p.product_name, b.dd FROM base b "
     "JOIN dim_product p ON b.product_id = p.product_id ORDER BY b.dd DESC", True),

    ("子查询派生表别名",
     "SELECT x.pid, x.s FROM (SELECT product_id AS pid, SUM(defect_qty) AS s "
     "FROM mes_process_output GROUP BY product_id) x ORDER BY x.s DESC", True),

    # ↓ 真幻觉：必须仍被拦下
    ("幻觉列名 total_defects_typo（schema 里没有、也不是别名）",
     "SELECT d.product_name, SUM(f.defect_qty) AS td FROM mes_process_output f "
     "JOIN dim_product d ON f.product_id = d.product_id "
     "GROUP BY d.product_name, f.not_exist_col ORDER BY td DESC", False),

    ("幻觉列 f.fake_metric",
     "SELECT d.product_name, SUM(f.fake_metric) FROM mes_process_output f "
     "JOIN dim_product d ON f.product_id = d.product_id GROUP BY d.product_name", False),

    ("幻觉列挂在别名表上 f.bad_col",
     "SELECT f.bad_col FROM mes_process_output f", False),
]

L = ["═══ _unknown_identifiers 修复验证 ═══"]
bad = 0
for desc, sql, expect_empty in CASES:
    got = LS._unknown_identifiers(sql)
    is_empty = not got
    ok = (is_empty == expect_empty)
    if not ok:
        bad += 1
    L.append(f"{'✅' if ok else '❌'} 期望{'通过(无未知)' if expect_empty else '拦下'} "
             f"实得 {sorted(got) if got else '无'}")
    L.append(f"     {desc}")

L.append("")
L.append(f"共 {len(CASES)} 例，不符 {bad} 例")
L.append("结论: " + ("通过 —— 别名不再误杀，真幻觉仍被拦下" if bad == 0 else "失败"))
open(r"C:\Users\overlxrd\WorkBuddy\2026-10-03-23-31-48\idfix.txt", "w", encoding="utf-8").write("\n".join(L))
print("\n".join(L))
sys.exit(1 if bad else 0)
