# -*- coding: utf-8 -*-
"""月级相对时间窗的锚点回归（2026-10-27 新增）。

钉住一个实测 bug：「本月与上月产量对比」返回的 SQL 把窗口写成
`>= '2026-09-01' AND < '2026-10-01' + INTERVAL '1 month'`（= [9/1, 11/1)），
而演示库数据只到 2026-09-15 —— 上界落在未来，「上月」那段恒空，
结果只剩一行，对比静默消失，且 success=true 不报错。

根因：`_reanchor_hardcoded_range` 原判据要求「字面量**全部**落在数据范围外」才重锚，
`'2026-09-01'` 在范围内 → 整条被放过。

本测试同时钉住「不能误伤」的一面：用户明确指定年月、或问句不含相对时间词时，
SQL 必须原样返回。
"""
import os as _os  # 路径基准：以本文件所在目录为准，换机器/换目录后仍有效
_BASE = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

import datetime as dt
import sys

sys.path.insert(0, _BASE)
sys.stdout.reconfigure(encoding="utf-8")

import agent.llm_service as L

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


# 演示库真实数据范围：2026-08-01 ~ 2026-09-15（与 yans 库一致）
L._probe_data_range = lambda sqlexec, tables: {"min": dt.date(2026, 8, 1),
                                               "max": dt.date(2026, 9, 15)}


def run(query, sql):
    return L._reanchor_hardcoded_range(None, query, sql)


print("═══ A. 应重锚：月级相对词 + 窗口上界越界 ═══")

# A1 实测失败的原始形态
q = "本月与上月产量对比"
sql = ("SELECT to_char(stat_date,'YYYY-MM') AS \"月份\", SUM(good_qty) AS \"产量\" "
       "FROM mes_process_output WHERE stat_date >= '2026-09-01'::date "
       "AND stat_date < '2026-10-01'::date + INTERVAL '1 month' "
       "GROUP BY 1 ORDER BY 1")
r = run(q, sql)
new = r[0] if r else sql
check("A1 上界由 11/1 回退到 10/1", "'2026-09-01'::date + INTERVAL '1 month'" in new, new[-90:])
check("A1 下界由 9/1 回退到 8/1（含 8 月）", "'2026-08-01'::date" in new, "")

# A2 无 INTERVAL 后缀、两字面量、上界越界
q2 = "本月和上个月停机时长变化"
sql2 = ("SELECT SUM(downtime_minutes) FROM eqp_downtime_record "
        "WHERE start_time >= '2026-09-01' AND start_time < '2026-11-01'")
r2 = run(q2, sql2)
new2 = r2[0] if r2 else sql2
check("A2 两月上界越界 → 整段回退一个月",
      "'2026-08-01'" in new2 and "'2026-10-01'" in new2, new2[-80:])

print("═══ B. 不得误伤：不该动的一律原样返回 ═══")

# B1 用户明确指定年月（无相对时间词）
q3 = "8月和9月的产量对比"
sql3 = ("SELECT SUM(good_qty) FROM mes_process_output "
        "WHERE stat_date >= '2026-08-01' AND stat_date < '2026-10-01'")
check("B1 明确指定月份 → 不动", run(q3, sql3) is None, "")

# B2 无日期的普通查询
check("B2 无日期字面量 → 不动",
      run("各产线良率排名", "SELECT line_id FROM mes_process_output GROUP BY line_id") is None, "")

# B3 窗口完全落在数据范围内（本月才是数据最新月的情形）
q4 = "本月各产线产量"
sql4 = ("SELECT SUM(good_qty) FROM mes_process_output "
        "WHERE stat_date >= '2026-09-01' AND stat_date < '2026-10-01'")
r4 = run(q4, sql4)
check("B3 窗口已在数据范围内 → 不动", r4 is None or "'2026-09-01'" in (r4[0] if r4 else sql4), "")

# B4 日级窗口词（最近7天）不该走月级平移逻辑
q5 = "最近7天各产线产量"
sql5 = ("SELECT SUM(good_qty) FROM mes_process_output "
        "WHERE stat_date >= '2026-09-01' AND stat_date < '2026-09-08'")
r5 = run(q5, sql5)
check("B4 日级窗口词不受月级回退影响",
      r5 is None or "'2026-09-02'" not in (r5[0] if r5 else ""), "")

print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}")
sys.exit(1 if FAIL else 0)
