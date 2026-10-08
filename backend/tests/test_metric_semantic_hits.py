# -*- coding: utf-8 -*-
"""口径命中守卫生效性测试（2026-10-06）。

三条新写口径必须真的能被 find_metrics / resolve_metric_intent 命中，
否则「写进注册表」等于白写——上游拿不到，下游照样错答。

另含本轮修掉的两个守卫缺陷的回归断言：
  ①「比较连词缺位」：「跟标准良率」的"跟"不在左邻剥离词表 → 标准良率命中被作废
  ②「排序语义被当WHERE」：句尾"差得最多"的「最多」触发比较词 skip → 整句口径被丢弃

运行：python tests/test_metric_semantic_hits.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

try:
    from agent.metric_registry import (
        find_metrics, resolve_metric_intent, get_all_metrics,
    )
except Exception as e:
    print("IMPORT FAIL:", e)
    sys.exit(1)

FAIL = 0
CASES = [
    # ── 三条新口径必须命中 ──
    ("按责任工序统计缺陷数量并降序排列", "责任工序缺陷数", "hit"),
    ("责任工序缺陷量是多少", "责任工序缺陷量", "hit"),
    ("检验良率排名", "检验良率", "hit"),
    # ── 回归①：比较连词「跟」不再误杀「标准良率」──
    ("各工序的检验良率是多少，跟标准良率比哪个差得最多", "标准良率", "hit"),
    # ── 回归②：句尾排序语义不再触发比较词 skip ──
    ("各工序的检验良率是多少，跟标准良率比哪个差得最多", "检验良率", "hit"),
    # ── 老口径不被带偏 ──
    ("各工序的良率是多少", "工序良率", "hit"),
    ("良率最低的产线是哪条", "良率", "hit"),
    ("一车间良率", "良率", "hit"),
    ("上个月产量", "产量", "hit"),
    # ── 未注册口径仍走 no_hit（防打扰红线）──
    ("各产品的库存周转天数", None, "no_hit"),
]

for q, expect_name, expect_status in CASES:
    hits = find_metrics(q)
    names = [h.get("name") for h in hits]
    r = resolve_metric_intent(q)
    st = r.get("status")
    ok = (st == expect_status) and (expect_name is None or expect_name in names)
    print("%s | %-46s exp(%s)=%-8s got=%-8s | find=%s"
          % ("OK  " if ok else "FAIL", q, expect_name, expect_status, st, names))
    if not ok:
        FAIL += 1

# ── 注册表条数下限：防止误删口径 ──
n = len(get_all_metrics())
print("\n用户口径总数: %d（下限 95，即 92 基线 + 3 条新口径）" % n)
if n < 95:
    print("FAIL: 口径数不足 95，疑似被误删")
    FAIL += 1

print("\n结果: %s" % ("ALL PASS" if not FAIL else "FAIL=%d" % FAIL))
sys.exit(1 if FAIL else 0)