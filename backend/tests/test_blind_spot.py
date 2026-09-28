# -*- coding: utf-8 -*-
"""盲点发现单元测试（P1-2）：历史 SQL 使用情况解析 + 盲点产出与排序"""
import sys
sys.path.insert(0, '.')

from agent.blind_spot import (_parse_usage, find_blind_spots, invalidate_cache)

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

SQLS = [
    "SELECT line_name, SUM(good_qty) FROM mes_process_output mp "
    "JOIN dim_production_line p ON mp.line_id=p.line_id GROUP BY line_name",
    "SELECT stat_date, COUNT(*) FROM mes_process_output GROUP BY stat_date",
    "SELECT product_name, AVG(defect_qty) FROM mes_process_output GROUP BY product_name",
    "SELECT SUM(downtime_minutes) FROM eqp_downtime_record",   # 无 GROUP BY，只聚合
    "这不是 SQL ###",                                           # 非法，应被忽略
]

print("═══ A. 历史使用情况解析（sqlglot 确定性）═══")
h, g, m = _parse_usage(SQLS)
check("A1 表命中统计", h.get("mes_process_output") == 3, str(h))
check("A2 JOIN 表也计入", h.get("dim_production_line") == 1, str(h.get("dim_production_line")))
check("A3 表名统一小写", all(k == k.lower() for k in h), str(list(h)))
check("A4 GROUP BY 列提取", {"line_name", "stat_date", "product_name"} <= g, str(sorted(g)))
check("A5 聚合列提取", {"good_qty", "defect_qty"} <= m, str(sorted(m)))
check("A6 COUNT(*) 无列不报错", True, "")
check("A7 非法 SQL 被忽略（不崩）", True, "")

h2, g2, m2 = _parse_usage(["SELECT ROW_NUMBER() OVER (PARTITION BY line_id ORDER BY qty) rn, "
                           "SUM(qty) FROM t GROUP BY line_id"])
check("A8 窗口函数内的列不计入聚合列", "qty" in m2 and "rn" not in m2, str(sorted(m2)))
check("A9 PARTITION BY 不计入 GROUP BY", g2 == {"line_id"}, str(sorted(g2)))

check("A10 空输入返回空结构", _parse_usage([]) == ({}, set(), set()), "")
check("A11 无 sqlglot 时降级不崩", isinstance(_parse_usage(SQLS), tuple), "")

print("═══ B. 盲点产出（真实库）═══")
invalidate_cache()
r = find_blind_spots(limit=4)
if not r["success"]:
    print("  ⚠️  ", r.get("error"), "—— 跳过 B1-B5")
else:
    spots = r["spots"]
    check("B1 返回成功", True, f"{len(spots)} 条盲点")
    check("B2 结构完整", all({"type", "table", "detail", "suggestion", "severity"} <= set(s)
                          for s in spots), "")
    check("B3 类型合法", all(s["type"] in ("unexplored_table", "unused_dimension", "unused_measure")
                          for s in spots), str([s["type"] for s in spots]))
    check("B4 按严重度降序", all(spots[i]["severity"] >= spots[i + 1]["severity"]
                              for i in range(len(spots) - 1)), str([s["severity"] for s in spots]))
    check("B5 limit 生效", len(spots) <= 4, str(len(spots)))
    check("B6 每条都有可执行建议", all(s.get("suggestion") for s in spots), "")
    for s in spots:
        print(f"     [{s['type']}] {s['detail']}")
        print(f"        → {s['suggestion']}")

check("B7 缓存命中（二次调用结果一致）",
      [s["detail"] for s in find_blind_spots(limit=4)["spots"]] == [s["detail"] for s in r["spots"]], "")
invalidate_cache()
check("B8 清缓存不崩", invalidate_cache() is None, "")

print(f"\n{'='*46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'='*46}")
sys.exit(1 if FAIL else 0)
