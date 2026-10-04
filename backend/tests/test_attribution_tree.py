# -*- coding: utf-8 -*-
"""多指标归因树单元测试（P1-1）：加性/乘性分开、贡献度加总、维度下钻、边界降级"""
import sys
sys.path.insert(0, '.')

from agent.attribution_tree import (build_attribution_tree, tree_to_text,
                                    _numeric_cols, _is_ratio_metric)

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

# 两期 × 两产线：L01 产量 1000→800（-200），L02 200→180（-20）
# 投入量同步下降；良率（比率）从 0.95→0.90 下降
ROWS = [
    {"stat_date": "2026-07-01", "line": "L01", "good_qty": 600, "input_qty": 1000, "yield_rate": 0.95},
    {"stat_date": "2026-07-01", "line": "L02", "good_qty": 100, "input_qty": 200, "yield_rate": 0.95},
    {"stat_date": "2026-08-01", "line": "L01", "good_qty": 480, "input_qty": 800, "yield_rate": 0.90},
    {"stat_date": "2026-08-01", "line": "L02", "good_qty": 90, "input_qty": 180, "yield_rate": 0.90},
]
RES = {"columns": ["stat_date", "line", "good_qty", "input_qty", "yield_rate"],
       "rows": ROWS, "row_count": 4}

print("═══ A. 指标识别 ═══")
cols = _numeric_cols(RES["columns"], "stat_date", ROWS)
check("A1 数值列识别（排除时间列）", "stat_date" not in cols and "good_qty" in cols, str(cols))
check("A2 比率指标识别（列名含率）", _is_ratio_metric("yield_rate", ROWS), "")
check("A3 取值 0-1 兜底判比率", _is_ratio_metric("pass_ratio_abc", ROWS), "")
check("A4 数量型不误判为比率", not _is_ratio_metric("good_qty", ROWS), "")

print("═══ B. 树结构 ═══")
t = build_attribution_tree("为什么产量下降了", RES)
check("B1 构建成功", t["success"], t.get("error", ""))
root = t["root"]
check("B2 主指标 = 问题中提到的产量列", root["name"] == "good_qty", root["name"])
check("B3 跨期变动正确", root["delta"] == -130, f"{root['prev']} → {root['cur']} = {root['delta']}")
check("B4 变动百分比", abs((root["pct_change"] or 0) - (-130 / 700 * 100)) < 0.1, str(root["pct_change"]))

add = [c for c in root["children"] if c.get("kind") == "additive"]
rat = [c for c in root["children"] if c.get("kind") == "ratio"]
check("B5 加性子节点识别（投入量）", any(c["name"] == "input_qty" for c in add), str([c["name"] for c in add]))
check("B6 比率子节点识别（良率）", any(c["name"] == "yield_rate" for c in rat), str([c["name"] for c in rat]))
check("B7 比率节点 contribution 为 None（不参与加和）",
      all(c.get("contribution_pct") is None for c in rat), "")
# 2026-10-04 更正：原断言是 `sum(contribution_pct) == 100`，实测得 -100 → 判失败。
# 根因是**测试没跟上 2026-10-03 那次修复**：那次把 contribution_pct 改成**带符号**
# （正=推高主指标，负=反向拖累），另加了 contribution_abs_pct 承载「绝对值占比」。
# 原因见 attribution_tree.py:312-318 —— 早期用 abs(delta) 算贡献度会让
# 「不良数上升 200」显示成「正贡献 60%」，符号与业务含义相反。
# 本例主指标 delta=-130（产量下降），子节点全为负，带符号之和自然 = -100%，
# 这**恰恰是正确的**；「绝对值占比合计 100%」由 contribution_abs_pct 承载。
s_signed = sum(c.get("contribution_pct") or 0 for c in add)
s_abs = sum(c.get("contribution_abs_pct") or 0 for c in add)
check("B8 加性贡献度绝对值占比加总 = 100%", abs(s_abs - 100.0) < 0.5,
      f"abs_pct={s_abs}% (signed={s_signed}%)")
# 符号方向必须与主指标一致：产量下降 → 所有加性子节点贡献度均为负
check("B8b 下降场景贡献度带负号", s_signed < 0,
      f"signed={s_signed}（主指标 delta={root['delta']}）")
check("B8c 带符号与绝对值占比逐项对应",
      all(abs(abs(c.get("contribution_pct") or 0) - (c.get("contribution_abs_pct") or 0)) < 0.05
          for c in add),
      str([(c["name"], c.get("contribution_pct"), c.get("contribution_abs_pct")) for c in add]))

print("═══ C. 维度下钻 ═══")
dims = [c for c in root["children"] if c.get("kind") == "dimension"]
check("C1 有维度分解节点", len(dims) > 0, str([(c["dim"], c["name"]) for c in dims]))
if dims:
    l01 = [c for c in dims if c["name"] == "L01"]
    check("C2 L01 变动量正确", l01 and l01[0]["delta"] == -120, str(l01[0]["delta"]) if l01 else "无")
    check("C3 负贡献最大排在前（变动主因优先）",
          dims[0]["delta"] <= dims[-1]["delta"], f"{dims[0]['delta']} <= {dims[-1]['delta']}")
    check("C4 子节点也下钻了维度",
          any(c.get("children") for c in add), str([len(c.get("children", [])) for c in add]))

print("═══ D. 文本渲染 ═══")
text = tree_to_text(root, ["2026-07-01", "2026-08-01"])
check("D1 包含下降结论", "下降" in text, text.splitlines()[0] if text else "")
check("D2 区分加性/比率说明", "可加和" in text and "不可加和" in text, "")
print("  ── 文本预览 ──")
for line in text.splitlines():
    print("   ", line)

print("═══ E. 边界与降级 ═══")
check("E1 无时间列降级", not build_attribution_tree("q", {"columns": ["a", "b"],
      "rows": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]})["success"], "")
check("E2 只有一期降级", not build_attribution_tree("q", {"columns": ["d", "v"],
      "rows": [{"d": "2026-01-01", "v": 1}]})["success"], "")
check("E3 无数值列降级", not build_attribution_tree("q", {"columns": ["d", "s"],
      "rows": [{"d": "2026-01-01", "s": "x"}, {"d": "2026-02-01", "s": "y"}]})["success"], "")
check("E4 空结果不崩", not build_attribution_tree("q", {"columns": [], "rows": []})["success"], "")
# 时间列名必须带时间语义（否则 _pick_time_col 不认），这里用 stat_date 走真实分支
check("E5 上期为 0 时降级（防除零）", not build_attribution_tree("q", {"columns": ["stat_date", "v"],
      "rows": [{"stat_date": "2026-01-01", "v": 0}, {"stat_date": "2026-02-01", "v": 5}]})["success"], "")
r = build_attribution_tree("q", {"columns": ["stat_date", "v"], "rows": [
    {"stat_date": "2026-01-01", "v": 100}, {"stat_date": "2026-02-01", "v": 130}]})
check("E6 上升也能归因（不只处理下跌）", r["success"] and "上升" in r["text"], r["text"].splitlines()[0])

print(f"\n{'='*46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'='*46}")
sys.exit(1 if FAIL else 0)
