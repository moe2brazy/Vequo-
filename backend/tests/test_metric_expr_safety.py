# -*- coding: utf-8 -*-
"""_safe_metric_expr 安全性回归：确认为 FILTER 放宽后注入面未扩大"""
import os, sys
sys.path.insert(0, r"D:\vequo\vequo-viqueo\backend")
os.chdir(r"D:\vequo\vequo-viqueo\backend")
from agent.metric_compiler import _safe_metric_expr as S

CASES = [
    # (表达式, 期望安全?)
    ("SUM(good_qty)", True),
    ("SUM(COALESCE(good_qty, 0))", True),
    ("SUM(good_qty) * 100.0 / NULLIF(SUM(input_qty), 0)", True),
    ("SUM(CASE WHEN snapshot_date = (SELECT MAX(snapshot_date) FROM inv_inventory_snapshot) "
     "THEN available_qty + COALESCE(frozen_qty,0) ELSE 0 END)", True),
    # ↓ 本次修复目标：合法的 FILTER 聚合子句
    ("SUM(available_qty + frozen_qty) FILTER (WHERE snapshot_date = "
     "(SELECT MAX(snapshot_date) FROM inv_inventory_snapshot))", True),
    ("SUM(downtime_minutes) FILTER (WHERE is_planned = FALSE)", True),

    # ↓ 注入面：必须仍被拒绝
    ("SUM(good_qty); DROP TABLE users", False),
    ("SUM(good_qty) -- comment", False),
    ("SUM(good_qty) /* x */", False),
    ("(SELECT password FROM users)", False),
    ("SUM((SELECT x FROM secrets))", False),
    ("SUM(a) UNION SELECT b FROM t", False),
    ("SUM(a) WHERE 1=1", False),
    ("SUM(a) FROM t GROUP BY b", False),
    ("SUM(CASE WHEN a=1 THEN (SELECT password FROM users) ELSE 0 END)", False),
    # FILTER 内夹带子查询注入 → 仍须拒绝
    ("SUM(a) FILTER (WHERE (SELECT password FROM users))", False),
    # FILTER 内夹带语句关键字 → 仍须拒绝
    ("SUM(a) FILTER (WHERE 1=1) ; DROP TABLE t", False),
    ("SUM(a) FILTER (WHERE EXISTS (SELECT 1 FROM secrets))", False),
]

L = ["═══ _safe_metric_expr 安全性回归 ═══"]
bad = 0
for expr, want in CASES:
    got = S(expr)
    ok = (got == want)
    if not ok:
        bad += 1
    L.append(f"{'✅' if ok else '❌'} 期望{'安全' if want else '拒绝'} 实得{'安全' if got else '拒绝'} | {expr[:95]}")

L.append("")
L.append(f"共 {len(CASES)} 例，不符 {bad} 例")
L.append("结论: " + ("通过 —— FILTER 放宽未扩大注入面" if bad == 0 else "失败 —— 存在安全回归"))
open(r"C:\Users\overlxrd\WorkBuddy\2026-10-03-23-31-48\sec.txt", "w", encoding="utf-8").write("\n".join(L))
sys.exit(1 if bad else 0)
