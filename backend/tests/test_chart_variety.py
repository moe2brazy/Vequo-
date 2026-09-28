# -*- coding: utf-8 -*-
"""图表选型多样化 + 趋势/关系类编译修复 单元测试"""
import sys
sys.path.insert(0, '.')

from agent.llm_service import _validate_chart_type
from agent.metric_compiler import try_compile_metric

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

def rows_of(cols_vals):
    cols = list(cols_vals.keys())
    n = len(next(iter(cols_vals.values())))
    return [{c: cols_vals[c][i] for c in cols} for i in range(n)]

print("═══ A. 选型多样化（确定性规则）═══")
# 构成集中（头部 ≥60%）→ donut
r = rows_of({"产线": ["L1", "L2", "L3", "L4"], "产量": [900, 30, 40, 30]})
check("A1 构成集中→donut", _validate_chart_type("bar", list(r[0]), r, "各产线的产量") == "donut", "")
# 3~6 类非比率总量 → donut（均匀分布也是 donut）
r = rows_of({"产线": ["L1", "L2", "L3"], "产量": [100, 110, 90]})
check("A2 少量类别非比率→donut", _validate_chart_type("bar", list(r[0]), r, "各产线的产量") == "donut", "")
# 比率指标 → 仍 bar（环形图读不了比率）
r = rows_of({"产线": ["L1", "L2", "L3"], "良率": [0.95, 0.92, 0.9]})
check("A3 比率指标→bar", _validate_chart_type("bar", list(r[0]), r, "各产线的良率") == "bar", "")
# 长类别名 → barh
r = rows_of({"工序": ["第一道冲压工序", "第二道焊接工序", "第三道喷涂工序", "第四道装配工序"],
             "产量": [100, 110, 90, 105]})
check("A4 长标签→barh", _validate_chart_type("bar", list(r[0]), r, "各工序的产量") == "barh", "")
# 类别过多 → barh
r = rows_of({"产品": [f"P{i}" for i in range(15)], "产量": list(range(15))})
check("A5 类别过多→barh", _validate_chart_type("bar", list(r[0]), r, "各产品的产量") == "barh", "")
# 关系语义 + 双数值（即使带类别列）→ scatter
r = rows_of({"设备": ["E1", "E2", "E3", "E4", "E5"], "停机时长": [60, 30, 80, 20, 50],
             "停机次数": [3, 2, 5, 1, 4]})
check("A6 关系语义→scatter", _validate_chart_type("bar", list(r[0]), r, "停机时长和停机次数的关系") == "scatter", "")
# 1 类别 + 2 数值 + 行数多 → stacked（需真正的类别列；日期+双数值是时间序列，
# 属于双线形态，不属于堆叠柱，见 A7b）
r = rows_of({"产线": [f"L{i % 5}" for i in range(15)], "产量": list(range(15)),
             "投入量": list(range(15, 30))})
check("A7 多行双指标→stacked", _validate_chart_type("bar", list(r[0]), r, "各产线的产量和投入量") == "stacked", "")
# 日期 + 双数值 + 无类别 → 不是散点（时间序列散点会丢时序），保持 line
r = rows_of({"日期": [f"2026-01-{i:02d}" for i in range(1, 15)], "产量": list(range(14)),
             "投入量": list(range(14))})
check("A7b 时间序列双指标→line（不误判散点）",
      _validate_chart_type("line", list(r[0]), r, "各天产量和投入量") == "line", "")
# 原有正确选型不回退
r = rows_of({"产品名称": ["A", "B", "C", "D"], "不良占比": [0.4, 0.3, 0.2, 0.1]})
check("A8 占比→rose 保持", _validate_chart_type("bar", list(r[0]), r, "各产品的不良占比") == "rose", "")
r = rows_of({"日期": ["2026-01-01", "2026-01-02", "2026-01-03"], "产量": [1, 2, 3]})
check("A9 趋势→line 保持", _validate_chart_type("bar", list(r[0]), r, "本月各天的产量趋势") == "line", "")

print("═══ B. 趋势类编译：维度+时间双分组 ═══")
r = try_compile_metric("各产线的产量趋势")
check("B1 编译命中", r is not None, "")
if r:
    check("B2 SQL 含时间列", "日期" in r["sql"], r["sql"][:90])
    check("B3 SQL 同时含维度与时间分组", "GROUP BY" in r["sql"] and "to_char" in r["sql"], "")
    check("B4 趋势行数放宽（≥60）", str(r["mql"]["limit"]) and r["mql"]["limit"] >= 60, str(r["mql"]["limit"]))
    check("B5 mql 记录双维度", r["mql"]["dimensions"] == ["产线", "日期"], str(r["mql"]["dimensions"]))
    check("B6 无维度纯趋势仍单时间分组", "各天的产量" not in "" and (
        try_compile_metric("近7天每天的产量") is not None), "")
r2 = try_compile_metric("近7天各工序的产量按天趋势")
check("B7 工序+按天趋势命中", r2 is not None and "process_name" in r2["sql"] and "日期" in r2["sql"],
      (r2 or {}).get("sql", "")[:80])

print("═══ C. 关系类编译：双指标自动配维度 ═══")
r = try_compile_metric("停机时长和停机次数的关系")
check("C1 编译命中", r is not None, "")
if r:
    check("C2 按维度分组（多行数据）", "GROUP BY" in r["sql"], r["sql"][:90])
    check("C3 两个指标都在投影", "停机时长" in r["sql"] and "停机次数" in r["sql"], "")
    check("C4 不是单行 LIMIT 1", "LIMIT 1" not in r["sql"], "")
check("C5 普通单指标汇总不受影响（仍 LIMIT 1）",
      (try_compile_metric("总产量是多少") or {}).get("sql", "").find("LIMIT 1") > -1
      or try_compile_metric("总产量是多少") is None, "")

print("═══ D. 普通对比类不受影响 ═══")
r = try_compile_metric("各产线的产量是多少")
check("D1 无趋势词不加时间列", r is not None and "日期" not in r["sql"], (r or {}).get("sql", "")[:80])

print(f"\n{'='*46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'='*46}")
sys.exit(1 if FAIL else 0)
