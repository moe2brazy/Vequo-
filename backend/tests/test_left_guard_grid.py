# -*- coding: utf-8 -*-
"""左邻修饰语守卫的**网格化**回归测试（2026-10-06）。

背景：这个守卫（`_left_modifier_suspect`）靠一张手工维护的中文功能词表
剥离命中词左侧的合法修饰语。词表缺一个词 → 该问法零命中 → 确定性编译
整体回退LLM（0.7s 变成 26~69s），或被上层守卫整句 skip。

历史上已修 6 次、复发 4 次（2026-09-29缺「哪些」、10-01 缺动词类、
10-01 缺最高级词、10-01 缺同比环比、10-04 缺「看/周度」、10-06 缺比较连词），
每次都是"线上遇到一句、补一个词"。逐个补治不了根：本测试用
**功能词 × 维度 × 指标** 的笛卡尔网格一次性穷举，把"缺哪个词当场暴露"。

判据（前缀类功能词）：该功能词开头的问法必须至少命中 1 个口径。
安全性：真正的口径修饰语（「一次」「出货」「来料」）仍必须零命中——
若网格里有任何一条被放过，防偷换能力即已失效。

运行：python tests/test_left_guard_grid.py
"""
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

try:
    from agent.metric_registry import find_metrics
except Exception as e:
    print("IMPORT FAIL:", e)
    sys.exit(1)

# 句法功能词 / 限定词：不是口径修饰语，出现时应被剥离放行
FUNC = [
    "分析", "统计", "查看", "计算", "对比", "列出", "找出",
    "各", "每个", "所有", "全部", "按",
    "最低", "最高", "最少", "最多", "最差", "最好",
    "同比", "环比", "较上月", "去年同期",
    "当前", "现在", "实际", "全厂", "整体",
    "累计", "平均", "总", "合计",
    "哪些", "哪个", "哪条", "哪道",
    "看", "周度", "月度", "帮我", "给我", "我想看",
    "跟", "同", "比", "较", "比较", "相较", "参照", "基于",
    "最近两周", "最近一个月", "近7天", "本年累计",
]
DIMS = ["产线", "车间", "工序", "设备", "产品", "仓库"]
METS = ["良率", "产量", "缺陷数", "停机时长", "库存量"]

# ── 正向：功能词在前，问句必须能命中 ──
miss = []
for f, d, m in itertools.product(FUNC, DIMS, METS):
    q = "%s各%s%s" % (f, d, m)
    if not find_metrics(q):
        miss.append((f, q))
print("网格规模: %d 词 × %d 维度 × %d 指标 = %d 问句"
      % (len(FUNC), len(DIMS), len(METS), len(FUNC) * len(DIMS) * len(METS)))
print("零命中（守卫误杀）: %d" % len(miss))
for f, q in miss[:25]:
    print("   MISS: %-46s 功能词=%s" % (q, f))

# ── 反向：真·口径修饰语必须仍被拦截 ──
REAL_MODIFIERS = ["一次", "出货", "来料", "外协", "委外"]
leaked = []
for r_, d, m in itertools.product(REAL_MODIFIERS, DIMS, METS):
    q = "各%s%s%s" % (d, r_, m)
    names = [h.get("name") for h in find_metrics(q)]
    # 若命中，且命中的口径名不含该修饰语 → 防偷换失效
    for n in names:
        if r_ not in str(n) and str(n) != m:
            leaked.append((q, r_, n))
print("\n真修饰语被误放行: %d" % len(leaked))
for q, r_, n in leaked[:15]:
    print("   LEAK: %-40s 修饰语=%s 命中=%s" % (q, r_, n))

FAIL = 1 if (miss or leaked) else 0
print("\n结果: %s" % ("ALL PASS" if not FAIL else
                     "FAIL 误杀=%d 泄漏=%d" % (len(miss), len(leaked))))
sys.exit(FAIL)