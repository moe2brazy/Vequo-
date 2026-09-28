# -*- coding: utf-8 -*-
"""会话记忆单元测试（P1-1）：实体提取/命中、短句判定、上下文构建"""
import sys
sys.path.insert(0, '.')

from agent.conv_memory import (extract_entities, entity_hits, is_anaphora_short,
                               build_conv_context)

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

ROWS = [
    {"产线": "L01", "产量": 100},
    {"产线": "L02", "产量": 200},
    {"产线": "L03", "产量": 300},
]

print("═══ A. 实体提取 ═══")
e = extract_entities(ROWS, ["产线", "产量"])
check("A1 提取维度实体", e.get("产线") == ["L01", "L02", "L03"], str(e))
check("A2 数值列被排除", "产量" not in e, str(e))
check("A3 日期列被排除", extract_entities(
    [{"日期": "2026-01-01", "v": 1}], ["日期", "v"]) == {}, str(extract_entities(
    [{"日期": "2026-01-01", "v": 1}], ["日期", "v"])))
check("A4 空输入返回空", extract_entities([], []) == {} and extract_entities(None, None) == {}, "")
e2 = extract_entities([{"名称": "一车间-1号线"}, {"名称": "一车间-2号线"}], ["名称"])
check("A5 中文实体", e2.get("名称") == ["一车间-1号线", "一车间-2号线"], str(e2))
check("A6 实体去重保序", extract_entities(
    [{"d": "A"}, {"d": "A"}, {"d": "B"}], ["d"])["d"] == ["A", "B"], "")

print("═══ B. 实体命中 ═══")
hits = entity_hits("那 L01 呢", {"产线": ["L01", "L02", "L03"]})
check("B1 命中实体", len(hits) == 1 and "L01" in hits[0] and "产线" in hits[0], str(hits))
check("B2 未命中返回空", entity_hits("各产品的产量", {"产线": ["L01"]}) == [], "")
check("B3 一列只报一条", len(entity_hits("L01和L02", {"产线": ["L01", "L02"]})) == 1, "")

print("═══ C. 短句判定（指代型）═══")
check("C1 那X呢", is_anaphora_short("那上个月呢") is True, "")
check("C2 那L01呢", is_anaphora_short("那L01呢") is True, "")
check("C3 再看不良", is_anaphora_short("再看看不良") is True, "")
check("C4 换成按月", is_anaphora_short("换成按月") is True, "")
check("C5 长句不是指代", is_anaphora_short("各产线的产量和不良率对比分析") is False, "")
check("C6 新主题指示词不是指代", is_anaphora_short("各产线产量") is False, "")
check("C7 空句/无上文", is_anaphora_short("", False) is False and is_anaphora_short("那呢", False) is False, "")

print("═══ D. 上下文构建 ═══")
ctx = build_conv_context(
    "那 L01 呢", prev_question="各产线的产量", prev_sql="SELECT ... GROUP BY 产线",
    prev_rows=ROWS, prev_cols=["产线", "产量"],
    prev_slots={"metrics": ["产量"]}, cur_slots={})
check("D1 含实体记忆", "L01" in ctx and "产线" in ctx, ctx[:80])
check("D2 含短句继承提示", "对上一轮查询的延续" in ctx, "")
check("D3 含指标继承", "产量" in ctx, "")

ctx2 = build_conv_context(
    "近7天各产线的产量", prev_question="各产线的产量", prev_sql="SELECT ...",
    prev_rows=ROWS, prev_cols=["产线", "产量"],
    prev_slots={"metrics": ["产量"], "time_range": {"key": "last_7_days", "n": 7}},
    cur_slots={"metrics": ["产量"]})
check("D4 新问题不带继承提示（有明确指示词）", "延续/修改" not in ctx2, ctx2[:60])

ctx3 = build_conv_context(
    "那上个月呢", prev_question="本月各产线的产量", prev_sql="SELECT ...",
    prev_rows=ROWS, prev_cols=["产线", "产量"],
    prev_slots={"metrics": ["产量"], "time_range": {"key": "this_month"}},
    cur_slots={"time_range": {"key": "last_month"}})
check("D5 时间范围变更被识别", "上个月" not in ctx3 or "时间范围" in ctx3, "")

check("D6 无上一轮返回空", build_conv_context("随便", prev_sql="", prev_rows=[], prev_cols=[]) == "", "")

print(f"\n{'='*46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'='*46}")
sys.exit(1 if FAIL else 0)
