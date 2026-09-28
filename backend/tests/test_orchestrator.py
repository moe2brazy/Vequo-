# -*- coding: utf-8 -*-
"""最小编排器（P0-A 演示版）单元测试：任务链规则 + 节点降级分支"""
import sys
sys.path.insert(0, '.')

from agent.orchestrator import plan_chain, orchestrate, _run_insight

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

print("═══ 任务链规则（plan_chain）═══")
cases = [
    ("为什么产量下降了，下个月会怎样", ["query", "attribution", "predict", "report"]),
    ("为什么停机时长上升", ["query", "attribution", "report"]),
    ("下个月产量预测", ["query", "predict", "report"]),
    ("有哪些异常风险点", ["query", "insight", "report"]),
    ("各产线的产量", ["query", "insight", "report"]),
    ("整体情况总结一下", ["query", "insight", "report"]),
]
for q, expect in cases:
    check(f"任务链: {q}", plan_chain(q) == expect, str(plan_chain(q)))

print("═══ 节点降级分支（不依赖 LLM/DB）═══")
r = _run_insight("test", {"status": "skipped"})
check("洞察节点依赖未完成 → skipped", r.get("status") == "skipped")
r = _run_insight("test", {"status": "done", "columns": ["a", "b"], "rows": [{"a": 1, "b": "x"}, {"a": 3, "b": "y"}]})
check("洞察节点数值统计", r.get("status") == "done" and "合计 4.00" in r.get("summary", ""), r.get("summary", "")[:60])
r = orchestrate("")
check("空问题 → error", r.get("success") is False)

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
