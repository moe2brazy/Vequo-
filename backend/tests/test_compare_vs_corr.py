# -*- coding: utf-8 -*-
"""回归测试：「对比」与「相关」的语义分流 + 异构量纲图表守卫（2026-10-04）

背景（实测缺陷）：
  用户问「产量和良率的对比」，系统答的是「皮尔逊相关系数 r=-0.11」，
  图表则渲染成「堆叠柱」——把 产量(件, ~1e5) 与 良率(%, ~97) 相加成一根柱子。

两个根因：
  ① llm_service._build_deterministic_insight 对**任何**分析意图都产出 Pearson 结论
     （「对比」被当成「相关」）；
  ② _validate_chart_type 规则 3b 只要「1 类别 + 2~3 数值 + 行多」就返回 stacked，
     不检查各数值列是否同量纲、可加总。

本测试锁死修复后的正确行为，防止回退。
"""
import sys
sys.path.insert(0, '.')

from agent.llm_service import (
    _validate_chart_type, _build_deterministic_insight,
    _is_corr_intent, _is_compare_intent, _mixed_ratio_and_count,
)

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def rows_of(cols_vals):
    cols = list(cols_vals.keys())
    n = len(next(iter(cols_vals.values())))
    return [{c: cols_vals[c][i] for c in cols} for i in range(n)]


print("=== A. 意图判定：对比 != 相关 ===")
check("A1 「对比」是 compare 不是 corr",
      _is_compare_intent("产量和良率的对比") and not _is_corr_intent("产量和良率的对比"), "")
check("A2 「是否相关」是 corr",
      _is_corr_intent("产量和良率是否相关") and not _is_compare_intent("产量和良率是否相关"), "")
check("A3 「A 和 B 的关系」是 corr",
      _is_corr_intent("停机时长和停机次数的关系"), "")
check("A4 同时含「对比…相关性」时以相关为准",
      _is_corr_intent("对比产量和良率的相关性"), "")

print("=== B. 分析文本：对比走对比、相关走 Pearson ===")
rows = [{"工序": f"P{i}", "产量": 50000 + i * 3000, "良率": 97.0 + (i % 7) * 0.12}
        for i in range(28)]
cols = list(rows[0].keys())
t_cmp = _build_deterministic_insight("产量和良率的对比", cols, rows) or ""
check("B1 对比题不出现「皮尔逊/相关系数」",
      ("皮尔逊" not in t_cmp) and ("相关系数" not in t_cmp), t_cmp[:60])
check("B2 对比题给出各指标量级",
      ("产量" in t_cmp) and ("良率" in t_cmp) and ("合计" in t_cmp or "均值" in t_cmp), "")
t_cor = _build_deterministic_insight("产量和良率是否相关", cols, rows) or ""
check("B3 相关题仍给皮尔逊",
      ("皮尔逊" in t_cor) and ("r=" in t_cor), t_cor[:60])
check("B4 趋势类不误算相关（返回 None 交 LLM）",
      _build_deterministic_insight("各工序的产量趋势", cols, rows) is None, "")

print("=== C. 图表量纲守卫：不同量纲不得堆叠 ===")
check("C1 产量+良率 判为混合量纲", _mixed_ratio_and_count(["产量", "良率"]), "")
check("C2 产量+投入量 非混合量纲", not _mixed_ratio_and_count(["产量", "投入量"]), "")
check("C3 产量+良率 全为比率 视为同量纲",
      not _mixed_ratio_and_count(["良率", "合格率"]), "")

r_mix = rows_of({"工序": [f"P{i}" for i in range(28)],
                 "产量": [50000 + i * 3000 for i in range(28)],
                 "良率": [97.0 + i * 0.05 for i in range(28)]})
check("C4 [工序]+产量+良率 28行 -> dual（双轴，不再 stacked）",
      _validate_chart_type("bar", list(r_mix[0]), r_mix, "产量和良率的对比") == "dual", "")

r_mix_d = rows_of({"日期": [f"2026-08-{i % 28 + 1:02d}" for i in range(28)],
                   "产量": [50000 + i * 3000 for i in range(28)],
                   "良率": [97.0 + i * 0.05 for i in range(28)]})
check("C7 [日期]+产量+良率 28行 -> dual",
      _validate_chart_type("bar", list(r_mix_d[0]), r_mix_d, "每日的产量和良率") == "dual", "")
check("C8 [日期]+产量+投入量 14行 -> 仍 line（同量纲不被 dual 抢走）",
      _validate_chart_type("line",
                           ["日期", "产量", "投入量"],
                           rows_of({"日期": [f"2026-01-{i:02d}" for i in range(1, 15)],
                                    "产量": list(range(14)), "投入量": list(range(14))}),
                           "各天产量和投入量") == "line", "")

r_same = rows_of({"产线": [f"L{i}" for i in range(28)],
                  "产量": [5000 + i * 30 for i in range(28)],
                  "投入量": [6000 + i * 30 for i in range(28)]})
check("C5 [产线]+产量+投入量 28行 -> 仍 stacked（原行为保持）",
      _validate_chart_type("bar", list(r_same[0]), r_same, "各产线的产量和投入量") == "stacked", "")

r_same2 = rows_of({"工序": [f"P{i}" for i in range(20)],
                   "产量": [5000 + i * 30 for i in range(20)],
                   "不良数": [60 + i for i in range(20)]})
check("C6 [工序]+产量+不良数 20行 -> 仍 stacked（原行为保持）",
      _validate_chart_type("bar", list(r_same2[0]), r_same2, "各工序的产量和不良数") == "stacked", "")

print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}")
sys.exit(1 if FAIL else 0)
