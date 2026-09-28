# -*- coding: utf-8 -*-
"""会话级记忆累积（多轮会话加深，对标 Spotter 3 conversational memory）测试。

覆盖 accumulate_session 的跨轮槽位合并/实体累积，build_session_context 的
跨轮实体命中/多轮槽位继承/轮次引用。全部确定性规则，可单测。
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = ""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}  {detail}")


def mk_history():
    """三轮对话：近7天产量 → 各产线 → 那 L01 呢"""
    return [
        {"role": "user", "content": "近7天各产线的产量"},
        {"role": "assistant", "content": "查询完成", "sql": "SELECT line_name AS 产线, SUM(good_qty) AS 产量 FROM mes_process_output WHERE stat_date >= CURRENT_DATE - 7 GROUP BY line_name",
         "columns": ["产线", "产量"],
         "rows": [{"产线": "L01", "产量": 100}, {"产线": "L02", "产量": 200}]},
        {"role": "user", "content": "那 L02 呢"},
        {"role": "assistant", "content": "查询完成", "sql": "SELECT line_name, SUM(good_qty) AS 产量 FROM mes_process_output WHERE stat_date >= CURRENT_DATE - 7 AND line_name = 'L02' GROUP BY line_name",
         "columns": ["产线", "产量"],
         "rows": [{"产线": "L02", "产量": 200}]},
        {"role": "user", "content": "那 L01 呢"},
    ]


print("═══ A. 跨轮槽位合并 ═══")
from agent.conversation_memory import accumulate_session, build_session_context

acc = accumulate_session(mk_history())
slots = acc["slots"]
check("A1 指标累积", "产量" in (slots.get("metrics") or []), str(slots))
check("A2 时间范围继承（第1轮设定仍生效）", slots.get("time_range") is not None, str(slots.get("time_range")))
check("A3 最近问题正确", acc["last_question"] == "那 L01 呢", acc["last_question"])
check("A4 最近 SQL 存在", bool(acc["last_sql"]), "")
check("A5 轮次数", len(acc["turns"]) == 2, str(len(acc["turns"])))

print("═══ B. 跨轮实体池（第1轮的实体第3轮命中） ═══")
ents = acc["entities"]
check("B1 实体池含 L01（来自第1轮）", "L01" in ents.get("产线", []), str(ents))
check("B2 实体池含 L02", "L02" in ents.get("产线", []), "")
ctx = build_session_context("那 L01 呢", acc)
check("B3 上下文含跨轮实体提示", "L01" in ctx and "之前某轮" in ctx, ctx[:150])

print("═══ C. 多轮槽位继承 ═══")
check("C1 上下文含时间继承", "近7天" in ctx or "时间范围" in ctx, ctx[:200])
check("C2 上下文含指标继承", "产量" in ctx, ctx[:200])

print("═══ D. 轮次引用 ═══")
acc2 = accumulate_session(mk_history())
ctx2 = build_session_context("把刚才那个改成按周", acc2)
check("D1 轮次引用触发", "最近一轮" in ctx2, ctx2[:150])
check("D2 引用含最近问题", "那 L02" in ctx2, ctx2[:200])

print("═══ E. 边界 ═══")
acc3 = accumulate_session([])
check("E1 空 history 不崩", acc3["slots"] == {} and acc3["turns"] == [], str(acc3))
ctx3 = build_session_context("", acc3)
check("E2 空 query 返回空串", ctx3 == "", "")
ctx4 = build_session_context("各产线产量", acc3)
check("E3 无历史上下文 → 空串", ctx4 == "", ctx4)

print("═══ F. 单轮退化（与 conv_memory 兼容） ═══")
acc5 = accumulate_session([
    {"role": "user", "content": "各产线的产量"},
    {"role": "assistant", "content": "ok", "sql": "SELECT line_name AS 产线, SUM(good_qty) AS 产量 FROM mes_process_output GROUP BY line_name",
     "columns": ["产线", "产量"], "rows": [{"产线": "L01", "产量": 100}]},
])
ctx5 = build_session_context("那 L01 呢", acc5)
check("F1 单轮实体也能命中", "L01" in ctx5 and "之前某轮" in ctx5, ctx5[:120])
ctx6 = build_session_context("那 L02 呢", acc5)  # L02 不在单轮结果里 → 不误报
check("F2 不存在的实体不误报", "L02" not in ctx6, ctx6[:120])

print("═══ G. 新主题检测（防旧过滤/时间污染新查询） ═══")
# 会话在聊产线产量并确定 L01 过滤，用户突然问不良数量（不同指标）
h7 = mk_history() + [
    {"role": "user", "content": "那 L01 呢"},
    {"role": "assistant", "content": "ok",
     "sql": "SELECT line_name AS 产线, SUM(good_qty) AS 产量 FROM mes_process_output WHERE line_name = 'L01' GROUP BY line_name",
     "columns": ["产线", "产量"], "rows": [{"产线": "L01", "产量": 100}]},
]
acc7 = accumulate_session(h7)
ctx7 = build_session_context("各产品的不良数量", acc7)
check("G1 新主题不继承 L01 过滤", "L01" not in ctx7, ctx7[:200])
check("G2 新主题不继承时间范围", "近7天" not in ctx7, ctx7[:200])
check("G3 新主题有切换提示", "新的分析主题" in ctx7, ctx7[:120])
# 延续修改句仍继承
ctx7b = build_session_context("改成按周看", acc7)
check("G4 延续句仍继承过滤", "L01" in ctx7b, ctx7b[:200])
check("G5 延续句不误判新主题", "新的分析主题" not in ctx7b, ctx7b[:120])

print("═══ H. 明确取消过滤（全部/所有） ═══")
acc8 = accumulate_session(h7 + [
    {"role": "user", "content": "所有产线的产量"},
    {"role": "assistant", "content": "ok",
     "sql": "SELECT line_name AS 产线, SUM(good_qty) AS 产量 FROM mes_process_output GROUP BY line_name",
     "columns": ["产线", "产量"], "rows": [{"产线": "L01", "产量": 100}]},
])
check("H1 取消过滤后 filters 清空", acc8["filters"] == {}, str(acc8["filters"]))
ctx8 = build_session_context("各产线的产量", acc8)
check("H2 不再注入 L01", "L01" not in ctx8, ctx8[:200])

print()
print("=" * 40)
print(f"通过 {_passed} 项，失败 {_failed} 项")
print("=" * 40)
