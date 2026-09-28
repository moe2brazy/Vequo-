# -*- coding: utf-8 -*-
"""快捷问题加权随机轮换测试（修复"推荐问题一成不变"）。

覆盖：
- 不同 seed → 组合不同（多样性）；
- 固定 seed → 可复现（确定性，测试可依赖）；
- 结果均为非空、无重复。
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


print("═══ A. seed 轮换多样性 ═══")
from agent.suggest import generate_guide_questions

outs = []
for s in range(1, 6):
    r = generate_guide_questions(limit=6, seed=s)
    qs = [x["question"] for x in r["questions"]]
    outs.append(qs)
    check(f"A{s} 返回 {len(qs)} 条非空", len(qs) >= 3 and all(q.strip() for q in qs), str(qs))

uniq = {tuple(o) for o in outs}
check("A6 5 个 seed 组合不完全相同", len(uniq) >= 3, f"{len(uniq)}/5 种组合")
check("A7 单组内无重复问题", all(len(set(o)) == len(o) for o in outs), "")

print("═══ B. 固定 seed 可复现（确定性） ═══")
r1 = generate_guide_questions(limit=6, seed=42)
r2 = generate_guide_questions(limit=6, seed=42)
check("B1 相同 seed 结果一致", [x["question"] for x in r1["questions"]] ==
      [x["question"] for x in r2["questions"]], "")

print("═══ C. 无 seed 走缓存（复用） ═══")
r3 = generate_guide_questions(limit=6)
r4 = generate_guide_questions(limit=6)
check("C1 无 seed 可复用（缓存）", [x["question"] for x in r3["questions"]] ==
      [x["question"] for x in r4["questions"]], "")

print("═══ D. 端点支持 seed ═══")
import main
r5 = main.suggest_questions_api(limit=4, seed=7)
check("D1 端点返回结构化条目", r5.get("questions") and all(
    "question" in q for q in r5["questions"]), str(r5)[:120])

print()
print("=" * 40)
print(f"通过 {_passed} 项，失败 {_failed} 项")
print("=" * 40)
