# -*- coding: utf-8 -*-
"""「比较词→skip」分支的**真判保全**测试（2026-10-06）。

背景：本轮把 resolve_metric_intent 里的比较词规则从
  「句中只要有比较词就 skip」
收窄为
  「比较词必须落在两个命中口径名之间才 skip」
因为 Q7「…跟标准良率比哪个差得最多」句尾的「最多」是排序语义，
却被当成 WHERE 过滤条件 → 整句 skip → 已命中的双口径全被丢弃。

收窄守卫最大的风险是**把真判一起放过**（"计划产量>实际产量"这类
真的 WHERE 条件若不再skip，会走到歧义澄清，阻断出数）。

本测试用「抽样构造A比较词B 问句 → 筛出真能命中 ≥2 口径的样本 →
 逐条确认仍= skip」的方式做保全，不靠人工列举、不靠想象。
样本集由代码现场生成，换注册表后仍有效。

运行：python tests/test_cmp_skip_no_regression.py
"""
import itertools
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

try:
    from agent.metric_registry import (
        get_effective_metrics, find_metrics, resolve_metric_intent,
    )
except Exception as e:
    print("IMPORT FAIL:", e)
    sys.exit(1)

CMPS = ["大于", "超过", "至少", "最多", ">"]
names = [str(m["name"]) for m in get_effective_metrics() if m.get("name")]

random.seed(20261006)
pairs = [(a, b) for a in names[:40] for b in names[:40] if a != b][:300]
_all = [(a, b) for a in names for b in names if a != b]
random.shuffle(_all)
pairs += _all[:300]

reachable, seen = [], set()
for a, b in pairs:
    for c in CMPS:
        q = "%s%s%s" % (a, c, b)
        if q in seen:
            continue
        seen.add(q)
        if len(find_metrics(q)) >= 2:
            reachable.append(q)

print("生效口径 %d 条；抽样构造 %d 个「A比较词B」问句" % (len(names), len(seen)))
print("其中真能命中 ≥2 口径（= 原skip 分支的真判样本）: %d 条" % len(reachable))

FAIL = 0
if not reachable:
    print("WARN: 未构造出任何真判样本，本测试本次无判别力（不判FAIL，但需留意）")
else:
    lost = []
    for q in reachable:
        st = resolve_metric_intent(q).get("status")
        if st != "skip":
            lost.append((q, st))
    print("收窄后仍正确 skip: %d/%d" % (len(reachable) - len(lost), len(reachable)))
    for q, st in lost[:10]:
        print("   FAIL 真判丢失: %-40s status=%s" % (q, st))
    FAIL += len(lost)

# ── 反向：句尾排序语义必须放行（本次修复目标）──
SORT_CASES = [
    ("各工序的检验良率是多少，跟标准良率比哪个差得最多", "hit"),
    ("各产线的良率是多少，哪个最低", "hit"),
    ("产量和良率哪个高", "hit"),
]
for q, exp in SORT_CASES:
    st = resolve_metric_intent(q).get("status")
    ok = st == exp
    print("%s | 排序语义 %-44s exp=%-5s got=%s"
          % ("OK  " if ok else "FAIL", q, exp, st))
    if not ok:
        FAIL += 1

print("\n结果: %s" % ("ALL PASS" if not FAIL else "FAIL=%d" % FAIL))
sys.exit(1 if FAIL else 0)