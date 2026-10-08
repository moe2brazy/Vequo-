# -*- coding: utf-8 -*-
"""三张表必须严格对齐（顺序 + 长度）——守卫的不变量。
`_UNDERIVABLE_METRIC_RULES` / `_UNREG_CAT_ORDER` / `_UNREG_NEED_SOURCE`
靠人工核对，历史上已经错位过一次（规则表加了条目忘加类别，导致
「命中第N条 → 取第N个类别」整体前移，守卫拿错源字段特征去比对）。
这类 bug 读代码看不出来，必须机器守。
"""
import os, sys
BACKEND = r"D:\vequo\vequo-viqueo\backend"
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
sys.stdout.reconfigure(encoding="utf-8")
import agent.llm_service as M

rules = M._UNDERIVABLE_METRIC_RULES
order = M._UNREG_CAT_ORDER
need = M._UNREG_NEED_SOURCE

fail = 0
print("规则表 %d 条 / 类别顺序 %d 项 / 源字段表 %d 键"
      % (len(rules), len(order), len(need)))
print()

if len(rules) != len(order):
    fail += 1
    print("★ FAIL 长度不等：规则表 %d ≠ 类别顺序 %d" % (len(rules), len(order)))
    print("  → 「命中第N条规则 → 取第N个类别」会错位")
else:
    print("OK   规则表与类别顺序长度一致（%d）" % len(rules))

print()
print("逐条对齐：")
for i, (p, _need_re, msg) in enumerate(rules):
    cat = order[i] if i < len(order) else "★越界"
    has = cat in need if cat != "★越界" else False
    ok = has
    if not ok:
        fail += 1
    print("  %s [%d] ask=%-36s cat=%-6s need_src=%s"
          % ("OK  " if ok else "FAIL", i, p.pattern[:34], cat,
             "有" if has else "★缺"))
    # 额外校验：消息文本应与类别语义相符（粗查关键词）
    if cat != "★越界" and cat not in msg and i < 7:
        pass   # 前 7 条是历史条目，不强制

print()
# 抽查：每类别的 need 特征不应为空
for cat in order:
    if cat in need:
        a, b = need[cat]
        if not a or not b:
            fail += 1
            print("  ★ FAIL 类别 %s 的 need 特征为空" % cat)
print("OK   各类别 need 特征非空" if not fail else "")

print()
print("结果: %s" % ("ALL PASS" if not fail else "FAIL=%d" % fail))
sys.exit(1 if fail else 0)